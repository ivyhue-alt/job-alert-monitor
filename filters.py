"""Scoring and filter logic, with no side effects on import.

Split out of job_alert.py so these can be tested. That file runs the
whole monitor at module level, so importing it to reach one function
was not possible - which is why three filter bugs reached production
before anything caught them.
"""
from datetime import datetime, timezone


MIN_SCORE = 5
MAX_AGE_DAYS = 30

CORE_AI = ["rlhf","annotation","annotator","data labeling","labeling","human data",
    "preference data","model evaluation","human-in-the-loop","hitl","ai trainer",
    "model behavior","content moderation","trust and safety","red team",
    "training data","human feedback","data quality","llm evaluation",
    "evaluation lead","applied ai","llm"]

CORE_OPS = ["ai operations","ai program","ai enablement","ai adoption",
    "data operations","annotation ops","quality manager","quality management",
    "quality analytics","quality analyst","automation"]

CORE = CORE_AI + CORE_OPS
# REMOVED from CORE: ai engineer / ml engineer / machine learning engineer /
# data engineer / platform engineer / backend engineer / research engineer /
# member of technical staff. Those were scoring +10 on titles that require a
# production-engineering stack, pushing unreachable roles to the top of the
# alert queue.
BROAD = ["program manager","project manager","quality","calibration","workforce",
    "vendor","sla","coordinator","operations manager","evaluation","management analyst","program analyst","quality assurance specialist","health insurance specialist","compliance specialist","health system specialist","program specialist","data analyst"]
DISQUALIFY = ["accountant","accounting","counsel","attorney","sales","designer",
    "payroll","compensation","recruiter","tax","billing","revenue","gtm",
    "creative","account executive"]

NYNJ = ["new york","new jersey","newark","jersey city","manhattan","brooklyn",
    ", nj","nyc",
    # ", ny" was a wildcard for the whole state, so Baldwinsville and
    # Albany read as NYC metro. An allowlist of near places is bounded;
    # a blocklist of far ones never can be.
    "queens","bronx","staten island","long island city",
    "white plains","yonkers","tarrytown","harrison, ny","purchase, ny",
    "rye, ny","new rochelle","elmsford","armonk","valhalla","hawthorne, ny",
    "westchester","mount vernon","scarsdale","port chester",
    "garden city","mineola","hempstead","westbury","melville","jericho",
    "syosset","great neck","lake success","new hyde park","uniondale",
    "hicksville","plainview","woodbury, ny","bethpage","farmingdale",
    "nassau county","suffolk county",
    "nanuet","pearl river","orangeburg","suffern","nyack","rockland county"]

# ", ny" matches the whole state, so Albany and Buffalo were passing as
# NYC metro. Checked BEFORE the include list, which returns early.
NY_TOO_FAR = ["albany","buffalo","rochester","syracuse","ithaca",
    "binghamton","utica","schenectady","troy, ny","niagara","watertown",
    "plattsburgh","elmira","corning","oswego","batavia","jamestown",
    "olean","saratoga","upstate","western new york"]
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
def score_tracks(job):
    """Return (ai_points, ops_points). Weights unchanged: a CORE term scores
    10 in the title and 3 in the description; BROAD scores 3 and 1. BROAD and
    the federal bonus are added to BOTH tracks rather than split - a program
    manager title supports an ops match and an AI-ops match equally."""
    t = (job.title or "").lower()
    if any(x in t for x in DISQUALIFY):
        return 0, 0
    d = (job.description or "").lower()
    def pts(words, hi, lo):
        n = 0
        for k in words:
            if k in t: n += hi
            elif k in d: n += lo
        return n
    shared = pts(BROAD, 3, 1) + fed_bonus(job)
    return pts(CORE_AI, 10, 3) + shared, pts(CORE_OPS, 10, 3) + shared

def track_of(ai, ops):
    """BOTH is the strongest signal - the role sits in the overlap the resume
    is actually written for - so it is reported first."""
    if ai >= MIN_SCORE and ops >= MIN_SCORE: return "BOTH"
    if ai >= MIN_SCORE: return "AI"
    if ops >= MIN_SCORE: return "OPS"
    return None

def score(job):
    ai, ops = score_tracks(job)
    return max(ai, ops)

def loc_ok(job):
    l = (job.location or "").lower()
    if any(f in l for f in FOREIGN): return False
    if any(x in l for x in NY_TOO_FAR): return False
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
