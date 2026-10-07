import json, os, time
from datetime import datetime, timezone
import httpx
from usajobs_public import fetch_public_federal
from filters import (
    MIN_SCORE, MAX_AGE_DAYS, CORE_AI, CORE_OPS, CORE, BROAD, DISQUALIFY,
    NYNJ, NY_TOO_FAR, FOREIGN, NON_NYNJ_US, FED_SERIES, MERIT_ONLY,
    fed_reject, fed_bonus, score_tracks, track_of, score, loc_ok, too_old)
from fitscore import hard_gate, llm_verdict_batch
from jobhive.scrapers import (GreenhouseScraper, AshbyScraper,
    WorkdayScraper, BuiltInScraper, TheMuseScraper, RemoteOKScraper,
    WeWorkRemotelyScraper, YCombinatorScraper)

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
if not DISCORD_WEBHOOK and os.path.exists("webhook.txt"):
    with open("webhook.txt") as f:
        DISCORD_WEBHOOK = f.read().strip()
STATE = "seen_jobs.json"
MAX_ALERTS_PER_RUN = 15
BATCH_SIZE = 12              # jobs per LLM request. The free tier counts
                             # REQUESTS, so batching is what makes this work:
                             # 240 jobs = 20 requests, not 240.
MAX_LLM_CALLS_PER_RUN = 2    # hourly x 24 = 48 requests/day, inside the
                             # free daily cap. At BATCH_SIZE 12 that is still
                             # 24 jobs scored per run, 576/day.   # requests, so 20 x 12 = up to 240 jobs per run
USE_LLM = True
LEGACY_BACKFILL_PER_RUN = 8  # migrated records were never LLM-scored. Drain a
                             # few per run instead of 500 in one go.



GREENHOUSE = ["anthropic","scaleai","labelbox","turing","invisibletech","snorkelai",
    "remotasks","invisible","databricks","datadog"]
ASHBY = ["mercor","openai","cohere"]
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



def collect(seen_urls):
    """Gate only. No LLM here - candidates are scored in batches afterwards."""
    hits = {}
    def keep(j):
        u = str(j.url)
        if u in seen_urls: return
        if too_old(j): return
        if fed_reject(j): return
        if not loc_ok(j): return
        ai_s, ops_s = score_tracks(j)
        s = max(ai_s, ops_s)
        if s < MIN_SCORE: return
        blocked = hard_gate(j.title, j.description)
        if blocked:
            print(f"[gate] {blocked[:50]:<50} | {(j.title or '')[:45]}")
            return
        hits[u] = {"score": s, "ai": ai_s, "ops": ops_s,
            "track": track_of(ai_s, ops_s), "title": j.title,
            "company": j.company, "location": (j.location or "")[:70],
            "posted": j.posted_at.strftime("%Y-%m-%d") if j.posted_at else "?",
            "url": u, "description": j.description or "",
            "verdict": "UNSCORED", "reason": "not yet scored", "gap": ""}
    def harvest(scraper, slug, kind, attempts=3, **kw):
        # A single transient failure used to discard an entire source: one
        # Workday endpoint returned 604 postings one run and raised on the
        # next, and the bare except made that look identical to an empty
        # board. Retry with backoff, and log an exhausted source as FAIL so
        # a real outage is distinguishable from a source with no matches.
        for n in range(1, attempts + 1):
            try:
                jobs = scraper(slug, **kw).fetch()
                if not jobs:
                    print(f"[WARN] {kind} {slug}: 0"); return
                print(f"[ok] {kind} {slug}: {len(jobs)}")
                for j in jobs: keep(j)
                return
            except Exception as e:
                if n < attempts:
                    print(f"[retry {n}/{attempts}] {kind} {slug}: {type(e).__name__}")
                    time.sleep(5 * n)
                else:
                    print(f"[FAIL] {kind} {slug}: {type(e).__name__} {str(e)[:60]}")
    for s in GREENHOUSE: harvest(GreenhouseScraper, s, "gh")
    for s in ASHBY: harvest(AshbyScraper, s, "ashby")
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

def save_state(state):
    """Temp file then atomic replace. A crash mid-write cannot leave
    seen_jobs.json truncated and crash the next run on json.load."""
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE)

def new_record(j, alerted):
    return {"status": "seen", "first_seen": TODAY, "verdict": j["verdict"],
            "reason": j["reason"], "gap": j.get("gap", ""),
            "title": j["title"], "company": j["company"],
            "alerted": alerted, "resume_version": "", "notes": ""}

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

first_run = not os.path.exists(STATE)
if first_run:
    seen = {}
else:
    seen = json.load(open(STATE, encoding="utf-8"))
    if isinstance(seen, list):
        print("!! seen_jobs.json is still a flat list.")
        print("!! Run: python migrate_state.py seen_jobs.json")
        raise SystemExit

# Skip re-scoring only jobs that have a REAL verdict. UNSCORED means the API
# failed or the budget ran out, so those must stay eligible for a retry or
# they are stuck as UNSCORED forever.
# Three buckets:
#   verdict in STRONG/STRETCH/NO -> settled, never re-score
#   verdict == "UNSCORED"        -> scoring failed, always retry
#   verdict missing/empty        -> legacy migrated record, never scored.
#                                   Drain a slice per run to respect quota.
settled, failed, legacy = set(), set(), []
for u, r in seen.items():
    v = r.get("verdict")
    if v in ("STRONG", "STRETCH", "NO"): settled.add(u)
    elif v == "UNSCORED":                failed.add(u)
    else:                                legacy.append(u)

legacy.sort()
backfill = set(legacy[:LEGACY_BACKFILL_PER_RUN])
skip = settled | (set(legacy) - backfill)

if failed or legacy:
    print(f"[queue] {len(failed)} failed-retry, {len(legacy)} legacy "
          f"({len(backfill)} this run), {len(settled)} settled")

current = collect(skip)
from collections import Counter
print("[tracks] " + ", ".join(f"{k}:{v}" for k, v in Counter(
    (j.get("track") or "none") for j in current.values()).most_common()))


def score_in_batches(cand):
    """Score gate-survivors in batches. Highest keyword score first, so if
    quota runs out it runs out on the least promising jobs."""
    if not USE_LLM or not cand:
        return
    # BOTH sits in the overlap the resume is written for, so it gets the
    # capped request budget before single-track jobs.
    order = sorted(cand, key=lambda u: (0 if cand[u].get("track") == "BOTH"
                                        else 1, -cand[u]["score"]))
    budget = MAX_LLM_CALLS_PER_RUN
    done = 0
    for i in range(0, len(order), BATCH_SIZE):
        if budget <= 0:
            print(f"[budget] request cap reached; {len(order)-done} left UNSCORED")
            break
        chunk = order[i:i + BATCH_SIZE]
        res = llm_verdict_batch([cand[u] for u in chunk])
        budget -= 1
        stop = False
        for u, v in zip(chunk, res):
            cand[u]["verdict"] = v["verdict"]
            cand[u]["reason"] = v.get("reason", "")
            cand[u]["gap"] = v.get("gap", "")
            if v.get("stop"): stop = True
        done += len(chunk)
        ok = sum(1 for u in chunk if cand[u]["verdict"] != "UNSCORED")
        print(f"[batch] {done}/{len(order)} scored ({ok}/{len(chunk)} ok), "
              f"{budget} requests left")
        if stop:
            print("[quota] daily Gemini quota exhausted - rest left for a later run")
            break


print(f"[gate-pass] {len(current)} candidates to score")
score_in_batches(current)

# Drop NO verdicts, but remember them so they are never re-scored.
for u in [u for u, j in current.items() if j["verdict"] == "NO"]:
    j = current.pop(u)
    print(f"[no] {j['reason'][:50]:<50} | {(j['title'] or '')[:40]}")
    seen[u] = {"status": "seen", "first_seen": TODAY, "verdict": "NO",
               "reason": j["reason"], "gap": "", "title": j["title"],
               "company": j["company"], "alerted": True,
               "resume_version": "", "notes": ""}

# description was only needed for scoring; do not persist it
for j in current.values():
    j.pop("description", None)

if first_run:
    ok = send(f"Monitor live. {len(current)} roles seeded.")
    if ok:
        save_state({u: new_record(j, True) for u, j in current.items()})
        print(f"Seeded {len(current)}. Confirmed.")
    else:
        print("Discord FAILED - state not seeded.")
else:
    RANK = {"STRONG": 0, "STRETCH": 1, "UNSCORED": 2}
    TAG = {"STRONG": "\U0001F7E2", "STRETCH": "\U0001F7E1"}

    # Newly scored this run, plus anything scored earlier that never got
    # alerted because it fell past the per-run cap. Backlog drains without
    # spending a second API call on it.
    backlog = [(u, r) for u, r in seen.items() if not r.get("alerted", True)]
    queue = [(u, j, True) for u, j in current.items()] + \
            [(u, r, False) for u, r in backlog]
    queue.sort(key=lambda x: (RANK.get(x[1].get("verdict"), 3),
                              -x[1].get("score", 0)))

    # Record every newly scored job immediately, alerted or not.
    for u, j in current.items():
        prev = seen.get(u, {})
        rec = new_record(j, prev.get("alerted", False))
        # never clobber your own tracking on a re-score
        for f in ("status", "resume_version", "notes", "first_seen"):
            if prev.get(f): rec[f] = prev[f]
        seen[u] = rec

    delivered = 0
    for u, j, is_new in queue[:MAX_ALERTS_PER_RUN]:
        gap = f"\ngap: {j['gap']}" if j.get("gap") else ""
        icon = TAG.get(j.get("verdict"), "\u26AA")
        if send(f"{icon} **{j.get('verdict')}** `[{j.get('track') or '?'}]` - {j.get('title')}\n"
                f"{j.get('company')} | {j.get('location','')} | "
                f"posted {j.get('posted','?')}\n"
                f"{j.get('reason','')}{gap}\n{u}"):
            seen[u]["alerted"] = True
            delivered += 1
        time.sleep(1)

    save_state(seen)
    counts = {}
    for _, j in current.items():
        counts[j["verdict"]] = counts.get(j["verdict"], 0) + 1
    remaining = sum(1 for r in seen.values() if not r.get("alerted", True))
    print(f"{len(current)} newly scored {counts} | {delivered} alerted | "
          f"{remaining} queued | {len(seen)} tracked")
