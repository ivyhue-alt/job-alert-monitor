"""
Shared fit scoring. Imported by BOTH job_alert.py and weekly/weekly_scan.py
so the two scanners can never drift apart again.

Two stages:
  1. hard_gate()  - free, regex/substring. Kills obvious misses before any
                    API call. This is what stops PhD research reqs.
  2. llm_verdict() - Gemini free tier. Only runs on gate survivors.

Env: GEMINI_API_KEY   (get one free at aistudio.google.com/apikey)
If the key is missing or the API fails, llm_verdict returns UNSCORED and
the job still alerts. Never silently drop a job because scoring broke.
"""
import json, os, re, time, pathlib, threading

_HERE = pathlib.Path(__file__).parent
PROFILE_PATH = _HERE / "profile.json"

# ------------------------------------------------------------- rate limiting
# The free tier limits requests per MINUTE and per DAY. Firing calls back to
# back inside one run trips the per-minute cap (429) within seconds. Space
# them out. 5s => max 12 calls/min, comfortably inside a 15 RPM ceiling.
MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL", "5.0"))
_last_call = [0.0]
_lock = threading.Lock()

def _throttle():
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()

# ---------------------------------------------------------------- hard gate

# Phrases that mean "you cannot get this job", full stop. Matched against
# title + description. Kept deliberately narrow: a false NO here is invisible
# to you, so only put things here you are certain about.
HARD_NO = [
    r"(require[ds]|must (have|hold)|minimum of)[^.]{0,40}\bph\.?d\b",
    r"(require[ds]|must (have|hold))[^.]{0,40}\bdoctorate\b",
    r"first[- ]author",
    r"\bpublication(s)? (in|at|record)\b",
    r"peer[- ]reviewed",
    r"\b(neurips|icml|iclr|acl|cvpr|emnlp)\b",
    r"\bpostdoc",
    r"published research",
    r"top[- ]tier (ml|ai) (conference|venue)",
    r"\b(8|9|10|12|15)\+? years of (software|engineering|research|ml|machine learning)",
    r"active (ts/sci|top secret|secret) clearance",
    r"\bpolygraph\b",
    r"must be (a )?(licensed|registered) (nurse|pharmacist|physician)",
    r"\b(rn|lpn|md|pharm\.?d)\b required",
    r"deep (expertise|experience) in (pytorch|tensorflow|jax)",
    r"design and train (neural|deep|large language)",
]

# Title words that are near-always wrong for this profile. Softer than HARD_NO:
# these only fire when NOT paired with a rescue word, so 'AI Quality Engineer'
# survives while 'Backend Engineer' does not.
TITLE_NO = ["software engineer", "backend engineer", "frontend engineer",
    "full stack", "fullstack", "devops", "site reliability", "sre",
    "research scientist", "research engineer", "member of technical staff",
    "data scientist", "machine learning engineer", "ml engineer",
    "security engineer", "platform engineer", "infrastructure",
    "accountant", "attorney", "counsel", "recruiter", "payroll", "tax",
    "account executive", "sales", "designer", "architect",
    "principal ", "staff ", "distinguished", "intern", "phd", "postdoc"]

TITLE_RESCUE = ["quality", "evaluation", "evaluator", "annotation", "rlhf",
    "human data", "trust and safety", "red team", "operations", "program",
    "analyst", "training data", "content"]

# Matched as regex with word boundaries, NOT substrings. "cto" as a plain
# substring matches "director" and silently killed every Director role.
# Required-stack rejection. If a posting REQUIRES a language she does not
# write, no amount of adjacent experience clears the screen. Matched only
# with requirement framing so a passing mention in a company blurb does not
# reject the job.
STACK = r"(golang|go lang|typescript|javascript|rust|java|c\+\+|c#|scala|kotlin|" \
        r"ruby|php|swift|react|node\.js|kubernetes|terraform)"
REQ = r"(require[ds]?|must have|proficien\w+|strong|expert\w*|deep|" \
      r"experience (with|in)|fluent|hands-on)"
STACK_NO = [
    rf"{REQ}[^.]{{0,60}}\b{STACK}\b",
    rf"\b{STACK}\b[^.]{{0,40}}{REQ}",
    r"\b\d\+? years[^.]{0,40}\b(software engineering|production code|"
    r"backend|full[- ]stack|distributed systems)\b",
    r"(build|ship|design)[^.]{0,40}\bproduction (code|services|systems)\b",
    r"\blive coding\b|\bcoding (screen|challenge|interview)\b",
    r"\bsystem design interview\b",
]

SENIORITY_NO = [r"\bvp of\b", r"\bvice president\b",
    r"\bhead of engineering\b", r"\bcto\b", r"\bchief technology\b",
    r"\bdirector of engineering\b", r"\bdirector of software\b"]


def _load_profile():
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return json.load(f)


def hard_gate(title, description):
    """Return None if the job passes, else a string reason it was rejected.
    Free. Runs on everything."""
    t = (title or "").lower()
    d = (description or "").lower()
    blob = t + " \n " + d

    for pat in SENIORITY_NO:
        m = re.search(pat, t)
        if m:
            return f"seniority: '{m.group(0)}' in title"

    if any(n in t for n in TITLE_NO) and not any(r in t for r in TITLE_RESCUE):
        bad = next(n for n in TITLE_NO if n in t)
        return f"title: '{bad}' with no quality/eval framing"

    for pat in HARD_NO:
        m = re.search(pat, blob)
        if m:
            return f"hard requirement: '{m.group(0)}'"

    # Required engineering stack she does not write. Only applied when the
    # title is engineering-flavoured OR the requirement wording is explicit,
    # so an ops role that merely mentions a stack is not rejected.
    for pat in STACK_NO:
        m = re.search(pat, d)
        if m:
            return f"required stack: '{m.group(0)[:38]}'"

    return None


# ---------------------------------------------------------------- llm stage

_PROMPT = """You screen job postings for one specific candidate. Be strict and \
realistic: a recruiter with 60 seconds and a stack of resumes is your standard, \
not an optimistic career coach.

CANDIDATE PROFILE
{profile}

JOB POSTING
Title: {title}
Company: {company}
Location: {location}
Description (truncated):
{desc}

Decide one verdict:
- STRONG  : candidate clears the stated requirements and would plausibly get a screen.
- STRETCH : candidate is missing something real but the gap is arguable or learnable, \
and applying is a reasonable use of time.
- NO      : a stated requirement the candidate does not have would get this rejected, \
or the role is simply the wrong function.

Return ONLY a JSON object, no markdown fences, no other text:
{{"verdict":"STRONG|STRETCH|NO","reason":"<max 20 words, name the single \
deciding requirement>","gap":"<the specific missing thing, or empty string>"}}"""


def llm_verdict(title, company, location, description, model="gemini-3.6-flash",
                retries=3):
    """Returns dict with verdict/reason/gap. Verdict 'UNSCORED' if the API is
    unavailable - caller should treat UNSCORED as 'alert anyway'."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return {"verdict": "UNSCORED", "reason": "no GEMINI_API_KEY set", "gap": ""}

    try:
        from google import genai
    except ImportError:
        return {"verdict": "UNSCORED", "reason": "google-genai not installed", "gap": ""}

    prof = _load_profile()
    prof.pop("_comment", None)

    prompt = _PROMPT.format(
        profile=json.dumps(prof, indent=1),
        title=title or "", company=company or "", location=location or "",
        desc=(description or "")[:6000])

    client = genai.Client(api_key=key)
    for n in range(retries + 1):
        try:
            _throttle()
            r = client.models.generate_content(model=model, contents=prompt)
            txt = (r.text or "").strip()
            txt = re.sub(r"^```(json)?|```$", "", txt, flags=re.M).strip()
            out = json.loads(txt)
            if out.get("verdict") in ("STRONG", "STRETCH", "NO"):
                return out
            return {"verdict": "UNSCORED", "reason": "bad verdict value", "gap": ""}
        except Exception as e:
            msg = str(e)
            transient = ("429" in msg or "RESOURCE_EXHAUSTED" in msg
                         or "503" in msg or "UNAVAILABLE" in msg)
            # A 429 on the DAILY quota will not clear by waiting 30s. Signal
            # it so the caller can stop spending the rest of the run.
            if "429" in msg and ("per day" in msg or "PerDay" in msg):
                return {"verdict": "UNSCORED", "reason": "daily quota exhausted",
                        "gap": "", "stop": True}
            if transient and n < retries:
                time.sleep(20 * (n + 1))
            elif n < retries:
                time.sleep(3 * (n + 1))
            else:
                return {"verdict": "UNSCORED",
                        "reason": f"{type(e).__name__}: {msg[:60]}", "gap": ""}


def assess(title, company, location, description, use_llm=True):
    """Full pipeline. Returns (verdict, reason, gap)."""
    blocked = hard_gate(title, description)
    if blocked:
        return "NO", blocked, ""
    if not use_llm:
        return "UNSCORED", "llm disabled", ""
    v = llm_verdict(title, company, location, description)
    return v["verdict"], v.get("reason", ""), v.get("gap", "")


if __name__ == "__main__":
    # smoke test - no API needed for the gate
    cases = [
        ("Research Scientist, Alignment", "PhD in ML required, first-author publications"),
        ("Senior Backend Engineer", "Build distributed services in Go"),
        ("AI Quality Engineer", "Design rubrics and review model outputs for accuracy"),
        ("Quality Manager, Data & Insights", "Contact center QA, SQL reporting, root cause analysis"),
        ("Manager, Annotation Operations", "Run a vendor annotation team, calibration, SLAs"),
    ]
    for t, d in cases:
        print(f"{(hard_gate(t, d) or 'PASS'):<50} | {t}")


# --------------------------------------------------------------- batch stage

_BATCH_PROMPT = """You screen job postings for one specific candidate. Be strict \
and realistic: a recruiter with 60 seconds and a stack of resumes is your \
standard, not an optimistic career coach.

CANDIDATE PROFILE
{profile}

You will be given {n} numbered job postings. Judge EACH one independently.

For each posting return one verdict:
- STRONG  : candidate clears the stated requirements and would plausibly get a screen.
- STRETCH : candidate is missing something real but the gap is arguable or \
learnable, and applying is a reasonable use of time.
- NO      : a stated requirement the candidate does not have would get this \
rejected, or the role is simply the wrong function.

POSTINGS
{postings}

Return ONLY a JSON array of exactly {n} objects, in the same order as the \
postings, no markdown fences and no other text:
[{{"i":1,"verdict":"STRONG|STRETCH|NO","reason":"<max 18 words, name the \
deciding requirement>","gap":"<specific missing thing, or empty string>"}}]"""


def llm_verdict_batch(jobs, model="gemini-3.6-flash", retries=3,
                      desc_chars=2000):
    """Score MANY jobs in ONE request. jobs is a list of dicts with keys
    title/company/location/description.

    The free tier limits REQUESTS, not tokens, so batching is the difference
    between 580 requests and 48. Returns a list the same length as jobs.

    On failure every entry comes back UNSCORED, and a "stop" key is set on
    the first entry if the daily quota is gone.
    """
    n = len(jobs)
    if n == 0:
        return []
    fail = lambda why, stop=False: [
        {"verdict": "UNSCORED", "reason": why, "gap": "",
         **({"stop": True} if stop and i == 0 else {})}
        for i in range(n)]

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return fail("no GEMINI_API_KEY set")
    try:
        from google import genai
    except ImportError:
        return fail("google-genai not installed")

    prof = _load_profile()
    prof.pop("_comment", None)

    blocks = []
    for i, j in enumerate(jobs, 1):
        blocks.append(
            f"--- POSTING {i} ---\n"
            f"Title: {j.get('title') or ''}\n"
            f"Company: {j.get('company') or ''}\n"
            f"Location: {j.get('location') or ''}\n"
            f"Description: {(j.get('description') or '')[:desc_chars]}")

    prompt = _BATCH_PROMPT.format(profile=json.dumps(prof, indent=1),
                                 n=n, postings="\n\n".join(blocks))

    client = genai.Client(api_key=key)
    for attempt in range(retries + 1):
        try:
            _throttle()
            r = client.models.generate_content(model=model, contents=prompt)
            txt = re.sub(r"^```(json)?|```$", "", (r.text or "").strip(),
                         flags=re.M).strip()
            arr = json.loads(txt)
            if not isinstance(arr, list):
                return fail("batch response not a list")
            # Map by the model's own index where present, else by position.
            out = [None] * n
            for pos, item in enumerate(arr):
                if not isinstance(item, dict):
                    continue
                idx = item.get("i")
                k = (idx - 1) if isinstance(idx, int) and 1 <= idx <= n else pos
                if k < n and out[k] is None:
                    v = item.get("verdict")
                    out[k] = {"verdict": v if v in ("STRONG","STRETCH","NO")
                                        else "UNSCORED",
                              "reason": str(item.get("reason", ""))[:200],
                              "gap": str(item.get("gap", ""))[:200]}
            for k in range(n):
                if out[k] is None:
                    out[k] = {"verdict": "UNSCORED",
                              "reason": "missing from batch response", "gap": ""}
            return out
        except Exception as e:
            msg = str(e)
            if "429" in msg and ("per day" in msg or "PerDay" in msg):
                return fail("daily quota exhausted", stop=True)
            transient = ("429" in msg or "RESOURCE_EXHAUSTED" in msg
                         or "503" in msg or "UNAVAILABLE" in msg)
            if attempt < retries:
                time.sleep((25 if transient else 4) * (attempt + 1))
            else:
                return fail(f"{type(e).__name__}: {msg[:60]}")
