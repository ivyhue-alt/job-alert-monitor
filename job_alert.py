import json, os, time
from datetime import datetime, timezone
import httpx
from usajobs_public import fetch_public_federal
from jobhive.scrapers import (GreenhouseScraper, AshbyScraper, LeverScraper,
    WorkdayScraper, BuiltInScraper, TheMuseScraper, RemoteOKScraper,
    WeWorkRemotelyScraper, YCombinatorScraper)

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
if not DISCORD_WEBHOOK and os.path.exists("webhook.txt"):
    with open("webhook.txt") as f:
        DISCORD_WEBHOOK = f.read().strip()
MIN_SCORE = 5
MAX_AGE_DAYS = 30
STATE = "seen_jobs.json"
MAX_ALERTS_PER_RUN = 15

CORE = ["rlhf","annotation","annotator","data labeling","labeling","human data",
    "preference data","model evaluation","human-in-the-loop","hitl","ai trainer",
    "model behavior","data operations","content moderation","trust and safety",
    "red team","quality analyst","training data","human feedback","data quality",
    "ai engineer","ml engineer","machine learning engineer","data engineer",
    "platform engineer","backend engineer","applied ai","llm","research engineer",
    "member of technical staff","pipeline","automation"]
BROAD = ["program manager","project manager","quality","calibration","workforce",
    "vendor","sla","coordinator","operations manager","evaluation","management analyst","program analyst","quality assurance specialist","health insurance specialist","compliance specialist","health system specialist","program specialist","data analyst"]
DISQUALIFY = ["accountant","accounting","counsel","attorney","sales","designer",
    "payroll","compensation","recruiter","tax","billing","revenue","gtm",
    "creative","account executive"]

NYNJ = ["new york","new jersey","newark","jersey city","manhattan","brooklyn",
    ", ny",", nj","nyc"]
FOREIGN = ["india","brussels","tokyo","japan","seoul","korea","dublin","ireland",
    "singapore","germany","brazil","canada","toronto","ottawa","montreal",
    "vancouver","waterloo","london"," uk ","australia","sydney","france","paris",
    "netherlands","spain","poland","mexico","israel","tel aviv","europe",
    "philippines","manila","non-us","stockholm","sweden","amsterdam","denmark",
    "norway","finland","switzerland","zurich","belgium","portugal","lisbon",
    "italy","romania","china","shanghai","hong kong","taiwan","argentina",
    "colombia","chile","emea","apac","latam"]
NON_NYNJ_US = ["san francisco","redwood city","seattle","denver","boston","austin",
    "dallas","chicago","los angeles","atlanta","miami","portland","nashville",
    ", ca",", wa",", ma",", co",", tx",", il",", ga",", fl",", or",", tn"]

GREENHOUSE = ["anthropic","scaleai","labelbox","turing","invisibletech","snorkelai",
    "remotasks","invisible","databricks","datadog"]
ASHBY = ["mercor","openai","cohere"]
LEVER = []
WORKDAY = [
    "https://pru.wd5.myworkdayjobs.com/Careers",
    "https://pfizer.wd1.myworkdayjobs.com/PfizerCareers",
    "https://jj.wd5.myworkdayjobs.com/JJ",
    "https://nyp.wd1.myworkdayjobs.com/nypcareers",
    "https://aig.wd1.myworkdayjobs.com/aig",
    "https://tiaa.wd1.myworkdayjobs.com/Search",
    "https://integralife.wd1.myworkdayjobs.com/Careers",
    "https://regeneron.wd1.myworkdayjobs.com/Careers",
    "https://bcbsa.wd1.myworkdayjobs.com/Careers",
    "https://bcbsnj.wd5.myworkdayjobs.com/hc",
    "https://blackstone.wd1.myworkdayjobs.com/blackstone_Careers",
    "https://shakeshack.wd5.myworkdayjobs.com/External",
    "https://burlington.wd5.myworkdayjobs.com/burlingtonCareers",
    "https://wonder.wd1.myworkdayjobs.com/WG",
]
AGGREGATORS = [
    ("builtin", {"max_pages": 40}),
    ("themuse", {"max_pages": 25}),
    ("remoteok", {}),
    ("weworkremotely", {}),
    ("ycombinator", {"max_company_pages": 15}),
]
AGG_MAP = {"builtin": BuiltInScraper, "themuse": TheMuseScraper,
           "remoteok": RemoteOKScraper, "weworkremotely": WeWorkRemotelyScraper,
           "ycombinator": YCombinatorScraper}

FED_SERIES = ["0343","2210","1910","0301","0360","0685","1101","0501"]
MERIT_ONLY = ["status candidates","current permanent federal","merit promotion",
    "current federal employees","competitive service employees",
    "current or former federal","land management eligible",
    "internal to the agency","agency employees only"]

def fed_reject(job):
    """True if a federal posting is not open to the public."""
    txt = ((job.description or "") + " " +
           str((job.raw or {}).get("QualificationSummary",""))).lower()
    return any(m in txt for m in MERIT_ONLY)

def fed_bonus(job):
    cats = (job.raw or {}).get("JobCategory") or []
    codes = [str(c.get("Code","")) for c in cats if isinstance(c, dict)]
    return 12 if any(x in FED_SERIES for x in codes) else 0
def score(job):
    t = (job.title or "").lower()
    if any(d in t for d in DISQUALIFY): return 0
    d = (job.description or "").lower()
    pts = 0
    for k in CORE:
        if k in t: pts += 10
        elif k in d: pts += 3
    for k in BROAD:
        if k in t: pts += 3
        elif k in d: pts += 1
    return pts + fed_bonus(job)

def loc_ok(job):
    l = (job.location or "").lower()
    if any(f in l for f in FOREIGN): return False
    if any(t in l for t in NYNJ): return True
    if "remote" in l: return True
    if any(c in l for c in NON_NYNJ_US): return False
    return bool(getattr(job, "is_remote", None))

def too_old(job):
    p = job.posted_at
    if not p: return False
    try:
        if p.tzinfo is None:
            p = p.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - p).days > MAX_AGE_DAYS
    except Exception:
        return False

def collect():
    hits = {}
    def keep(j):
        if too_old(j): return
        if fed_reject(j): return
        s = score(j)
        if loc_ok(j) and s >= MIN_SCORE:
            hits[str(j.url)] = {"score": s, "title": j.title,
                "company": j.company, "location": (j.location or "")[:70],
                "posted": j.posted_at.strftime("%Y-%m-%d") if j.posted_at else "?",
                "url": str(j.url)}
    def harvest(scraper, slug, kind, **kw):
        try:
            jobs = scraper(slug, **kw).fetch()
            if not jobs:
                print(f"[WARN] {kind} {slug}: 0"); return
            print(f"[ok] {kind} {slug}: {len(jobs)}")
            for j in jobs: keep(j)
        except Exception as e:
            print(f"[skip] {kind} {slug}: {str(e)[:60]}")
    for s in GREENHOUSE: harvest(GreenhouseScraper, s, "gh")
    for s in ASHBY: harvest(AshbyScraper, s, "ashby")
    for s in LEVER: harvest(LeverScraper, s, "lever")
    for s in WORKDAY: harvest(WorkdayScraper, s, "workday")
    for name, kw in AGGREGATORS:
        harvest(AGG_MAP[name], "any", f"agg-{name}", **kw)
    for j in fetch_public_federal(): keep(j)
    return hits

def send(msg):
    try:
        r = httpx.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=15)
        if r.status_code in (200, 204): return True
        print(f"[discord ERR] {r.status_code} {r.text[:80]}")
    except Exception as e:
        print(f"[discord ERR] {e}")
    return False

if not DISCORD_WEBHOOK.startswith("https://discord.com/api/webhooks/"):
    print("!! webhook.txt invalid."); raise SystemExit

def save_state(keys):
    """Temp file then atomic replace. A crash mid-write cannot leave
    seen_jobs.json truncated and crash the next run on json.load."""
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sorted(keys), fh)
    os.replace(tmp, STATE)

current = collect()
first_run = not os.path.exists(STATE)
seen = set() if first_run else set(json.load(open(STATE)))

if first_run:
    ok = send(f"Monitor live. {len(current)} roles seeded.")
    if ok:
        save_state(current.keys())
        print(f"Seeded {len(current)}. Confirmed.")
    else:
        print("Discord FAILED - state not seeded.")
else:
    new = [(u, j) for u, j in current.items() if u not in seen]
    new.sort(key=lambda x: -x[1]["score"])
    delivered = set()
    for u, j in new[:MAX_ALERTS_PER_RUN]:
        if send(f"**{j['title']}**\n{j['company']} | {j['location']} | "
                f"posted {j['posted']} | score {j['score']}\n{j['url']}"):
            delivered.add(u)
        time.sleep(1)
    # Union, not intersect. A scraper that errors drops its jobs from
    # current; pruning against current forgets them and re-alerts next run.
    new_state = seen | delivered
    save_state(new_state)
    print(f"{len(new)} new, {len(delivered)} delivered.")
