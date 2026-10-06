# job-alert-monitor

Hourly job monitoring across 33 sources, with weighted relevance scoring, state-based deduplication, and delivery-confirmed alerting to Discord.

## Problem

Job boards resurface the same postings for weeks and bury relevant ones under volume. Checking dozens of sources by hand is unreliable and doesn't scale. The harder problem is that a monitor which alerts twice is annoying, but one that silently drops a posting is useless — and naive state handling produces the second failure mode.

## Coverage

**ATS platforms:** Greenhouse, Ashby, Workday
**Aggregators:** BuiltIn, TheMuse, RemoteOK, WeWorkRemotely, YCombinator
**Federal:** USAJOBS, via a custom direct-API client

33 configured sources, roughly 11,000 postings per full run.

## Sample run

```
[ok] gh anthropic: 459
[ok] gh databricks: 805
[ok] ashby openai: 738
[ok] workday https://jj.wd5.myworkdayjobs.com/JJ: 1805
[ok] workday https://regeneron.wd1.myworkdayjobs.com/Careers: 604
[ok] agg-ycombinator any: 832
[ok] agg-weworkremotely any: 685
[ok] usajobs-public: 265
Seeded 291. Confirmed.
```

A transient failure, retried and recovered:

```
[retry 1/3] workday regeneron: ScraperError
[retry 2/3] workday regeneron: ScraperError
[ok] workday regeneron: 604
```

Before retries were added, that source returned 604 postings one run and raised on the next, and a bare `except` logged it identically to an empty board - 604 postings dropped with no signal that anything went wrong.

~11,000 postings in, 291 matches out — a 2.6% pass rate. Tuning that ratio is the ongoing work: too loose and the alerts stop being read, too tight and the system quietly hides the thing you were watching for.

## The USAJOBS client

The scraper library ships a USAJOBS scraper. I tested it and replaced it with a direct client against `data.usajobs.gov`, because the library's version didn't expose the filters the federal system actually needs:

- **`HiringPath=public`** — without it, results are dominated by postings open only to current federal employees, which are noise for an external applicant.
- **Occupational series targeting** (0343, 2210, 1910, 0301, 0685, 1101, 0501, 0360) — federal roles are classified by series, not job title. Title-keyword matching both misses correctly-matching roles and returns wrong ones.
- **Geographic restriction** to NJ, NY, CT, PA, MD, VA and DC, plus explicitly remote postings.

**Three independent checks for public eligibility, not one.** The API's `HiringPath` filter still returns merit-promotion postings, so each result is re-checked against its `WhoMayApply` field, and again against a phrase list scanning the qualification text (`current federal employees`, `merit promotion`, `status candidates`, and similar). Any one of the three can reject a posting. The second and third layers exist because the API's own filter let restricted postings through.

## Design decisions

**State is written only after delivery is confirmed.** Recording a posting as "seen" before the webhook returns success means one network failure drops it permanently — silent data loss, invisible until you notice you never heard about a job. Writing state after confirmation trades a possible duplicate for zero loss. Duplicates are cheap; misses are not.

**State writes are atomic.** Written to a temp file, then `os.replace()`. A crash mid-write would otherwise leave a truncated `seen_jobs.json` that crashes the next run on load — taking the monitor down at exactly the moment nobody is watching it.

**Seen-state is a union, never an intersection.** The obvious pruning approach — keep only what's still in the current run — breaks when a scraper errors and its postings vanish from that run. They'd be forgotten and re-alerted next time. The set only grows; at this volume that's the right trade.

**Weighted scoring, not binary keyword matching.** Core terms score 10 on a title match, 3 in the description; broad terms score 3 and 1. Federal postings in a targeted occupational series get a further bonus. Binary matching gave high recall and unusable precision. A separate disqualification list removes titles that score well but are categorically wrong.

**Timezone-aware datetimes throughout,** with a 30-day age filter. Naive datetimes across sources returning different formats produced comparison errors and let stale postings through.

**Per-run alert cap with a sorted queue.** Capped at 15, highest score first, with a delay between sends. An unbounded loop over a first run would hit the webhook rate limit and lose alerts to failed sends.

**Credentials never in source.** API key and User-Agent come from environment variables; the webhook URL from an env var or a gitignored local file, with format validation before first use.

## Stack

Python 3.13, `httpx`, `jobhive-py`. Runs hourly via Windows Task Scheduler with `MultipleInstances=IgnoreNew` and a 30-minute execution limit, so a slow run can't stack instances.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env        # add your own values
python job_alert.py
```

Requires a free USAJOBS developer API key (developer.usajobs.gov) and a Discord webhook URL. The first run seeds state and sends a single summary rather than alerting on every existing posting.

## Known limitations

- **Two deployments currently run as forked copies rather than config profiles.** A second instance monitors a different keyword and location set, which means maintaining two divergent copies of one file. Moving source lists, keyword weights, and geography into per-profile config files is the next planned change.
- **No test suite.** Everything executes at module level, so the scoring and filter functions can't be imported and tested in isolation. Extracting them is a prerequisite for the config refactor.
- **Scoring weights were tuned by hand** against observed results, not measured against a labeled set.
- **Exceptions are caught broadly per source** so one failing endpoint can't kill a run. The cost is that a source silently returning nothing looks much like a source that is simply empty.
- **Scheduling is host-dependent.** Containerizing with a cron runner would make it portable.

## What I'd do differently

Build the config layer first. Every source added since has had to be added twice, and the cost compounds with each one.
