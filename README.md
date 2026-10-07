# job-alert-monitor

[![tests](https://github.com/ivyhue-alt/job-alert-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/ivyhue-alt/job-alert-monitor/actions/workflows/tests.yml)

Scans ~11,000 job postings across 33 sources on an hourly schedule and alerts on the few worth reading. Every posting it rejects, it rejects with a printed reason.

That last part is deliberate. A filter that drops things silently is a filter you can't debug — two real bugs in this repo were found by reading rejection logs, not by testing.

## Pipeline

```
harvest  →  hard gate  →  keyword score  →  LLM adjudication  →  alert
 33 src      free           two tracks        batched, capped     Discord
 ~11,000     regex          AI / OPS          STRONG/STRETCH/NO
```

Each stage costs more than the one before it, so the expensive stage sees as little as possible. Only postings that are new since the last run are scored at all; the rest are already in state.

### 1. Harvest

**ATS:** Greenhouse, Ashby, Workday · **Aggregators:** BuiltIn, TheMuse, RemoteOK, WeWorkRemotely, YCombinator · **Federal:** USAJOBS, through a custom direct-API client.

A source that fails transiently is retried with backoff. One that exhausts its retries logs `FAIL`, which is deliberately distinct from a source that legitimately returned nothing.

```
[ok] gh anthropic: 641
[ok] gh databricks: 886
[ok] ashby openai: 823
[retry 1/3] workday regeneron: ScraperError
[retry 2/3] workday regeneron: ScraperError
[ok] workday regeneron: 604
[ok] usajobs-public: 266
```

Before retries existed, that Workday endpoint returned 604 postings one run and raised on the next — and a bare `except` logged the failure identically to an empty board. 604 postings dropped with nothing to indicate anything had gone wrong.

### 2. Hard gate

Regex and substring checks. Free, runs on everything, and returns a reason string rather than a boolean:

```
[gate] title: 'software engineer' with no quality/eval framing | Staff+ Software Engineer, Distributed Systems
[gate] hard requirement: 'iclr'                                | Forward Deployed AI Engineer
[gate] required stack: 'proficient in python, javascript,'     | Applied AI Engineer, API Platform
[gate] title: 'architect' with no quality/eval framing         | Manager, Applied AI Architect
```

Four rule families:

| Rule | Fires on | Example |
|---|---|---|
| `SENIORITY_NO` | Titles above the target band | `VP of`, `Head of Engineering`, `CTO` |
| `TITLE_NO` + `TITLE_RESCUE` | Wrong function — unless a rescue word is present too | `Backend Engineer` is out; `AI Quality Engineer` survives |
| `HARD_NO` | Stated requirements that can't be met | required PhD, publication record, active clearance |
| `STACK_NO` | A required language or stack, matched **only with requirement framing** | `proficient in TypeScript` rejects; a passing mention in a company blurb doesn't |

`TITLE_RESCUE` is what keeps this from being a blunt keyword filter. A blocked title survives if it also carries a rescue word, so rejection happens on function rather than on vocabulary.

### 3. Keyword score, split into two tracks

Core terms score 10 on a title match and 3 in the description; broad terms score 3 and 1. The core list is split into an **AI** track and an **OPS** track, scored separately, so the alert carries a label saying which kind of role it is — and therefore which résumé to send.

Broad terms and the federal series bonus count toward **both** tracks rather than being divided between them. A "Program Manager" title supports an ops match and an AI-ops match equally; splitting it would under-score exactly the overlap roles that matter most.

`BOTH` means the posting cleared the threshold on both tracks. It's rare — across 685 tracked postings, no title contained terms from both lists, so it only fires on description overlap — and it gets first claim on the capped LLM budget.

### 4. LLM adjudication

The free tier caps **requests**, not tokens, so postings are batched 12 to a request: 240 jobs costs 20 requests instead of 240. Candidates are ordered by track and keyword score first, so if the budget runs out it runs out on the weakest ones.

Three verdicts: `STRONG` (clears the stated requirements), `STRETCH` (a real gap, but arguable or learnable), `NO` (a stated requirement the candidate doesn't have). Each comes back with a reason capped at 18 words naming the single deciding requirement, plus the specific gap. `UNSCORED` is not a fourth verdict. It means the posting cleared the gate and the keyword score, reached the LLM stage, and the request was never made: the daily quota was spent, or the API returned an error. It says nothing about the job itself. The posting stays in the retry queue and is scored on a later run rather than guessed at.

The alert still goes out. On a day when the quota breaks, suppressing `UNSCORED` would mean silence, hiding postings that the cheaper stages already judged worth reading, with no sign anything had been withheld. An honest "couldn't evaluate this" beats nothing.

**A cost bug worth writing down.** The per-run request cap was 20. On an hourly schedule that's **480 requests a day**, which exhausts a free daily quota well before noon — after which every alert read `UNSCORED: daily quota exhausted` instead of carrying a verdict. Capping at 2 per run gives 48 a day, inside the quota, while still scoring up to 24 jobs per run. The per-run number looked reasonable in isolation. The per-day number was the one that mattered, and nothing in the code was computing it.

Rate limiting is a separate problem with a separate fix: calls are spaced 5 seconds apart, because firing them back to back trips the per-minute ceiling within seconds of starting.

## The candidate profile is a file, not code

`profile.json` holds everything the scorer knows. The schema is in `profile.example.json`; the real one is gitignored.

- **`has`** — capabilities, named the way a posting would name them.
- **`lacks`** — 27 entries, deliberately unflattering. This is the field that prevents false positives, and the near-misses do the most work: "benefit administration, NOT sales"; "has SQL, has never written SAS"; "retail supervision, not corporate people management."
- **`notes_for_scorer`** — adjudication rules. The one that carries the most weight:

> Distinguish **REQUIRED** from **PREFERRED**. A posting stating it requires something listed in `lacks` is a NO. The same thing marked "preferred", "a plus" or "nice to have" is at worst a STRETCH.

Without that rule the screen rejects nearly everything, because almost every posting lists something the candidate doesn't have. With it, the question becomes whether the posting treats that thing as a wall or a wish.

## What the rejection reasons exposed

Neither was found by testing. Both were found by reading output that already existed.

**1. `go deep` parsed as a Go-language requirement.**

```
[gate] required stack: 'go deep' | Security Risk Analyst, Risk Engineering
```

The stack pattern matched `\bgo\b` and the requirement pattern included `deep`, so "go deep" read as a stack requirement. The same combination would have caught "go to market", "go live" and "go-forward". Fixed by matching `golang` rather than bare `go`, which loses a few genuine Go requirements — those postings almost always say Golang or list it beside another language that still matches.

**2. A Ph.D. mention killed four federal postings in one run.**

```
[gate] hard requirement: 'ph.d' | Quality Assurance Specialist
[gate] hard requirement: 'ph.d' | Quality Assurance Specialist
[gate] hard requirement: 'ph.d' | Management and Program Analyst
```

Federal postings list a doctorate as one qualifying path among several in the education-substitution block, not as a requirement. The pattern matched the bare mention. Those were on-target roles being dropped before they were ever scored. Fixed by requiring requirement-framing near the degree — the same discipline `STACK_NO` already used, which the degree patterns had simply never been held to.

A silent filter would have hidden both of these indefinitely.

## The same bug keeps coming back

Four times now a rule has matched text it was never meant to match:

| Pattern | What it also matched | Cost |
|---|---|---|
| `cto` | "dire**cto**r" | every Director role, silently |
| `\bgo\b` near requirement words | "go deep" | a Security Risk Analyst posting |
| `ph.d` | an education-substitution line | four federal roles in one run |
| `, ny` | the entire state | a posting four hours away |

Three surfaced from rejection reasons. The fourth surfaced from noticing that no
Director role had ever appeared, which took considerably longer.

The repair was the same shape each time: require a word boundary, require
requirement framing, replace a blocklist with an allowlist. The lesson is that
the near set is usually small and bounded while the far set never is - so an
allowlist of what counts beats a blocklist of what doesn't.

A filter built on substrings will keep doing this. Printing the reason is what
makes that survivable.

## Other design notes

**State writes are atomic.** Temp file, then `os.replace()`. A crash mid-write would otherwise leave a truncated state file that crashes the next run on load, taking the monitor down at exactly the moment nobody is watching it.

**An alert is only marked delivered once the webhook confirms it.** Scored postings are recorded immediately whether or not they alert, but the `alerted` flag is set only after a successful send — so a failed webhook leaves the posting queued rather than silently consumed.

**Unalerted postings drain from a backlog** instead of being re-scored. Anything that fell past the per-run alert cap surfaces on a later run without spending a second API call.

**Three independent checks for federal public eligibility.** The API's own `HiringPath=public` filter still returns merit-promotion postings, so each result is re-checked against its `WhoMayApply` field, then again against a phrase list in the qualification text. Any one of the three can reject. Layers two and three exist because layer one let restricted postings through.

**Federal roles are matched by occupational series**, not title — 0343, 2210, 1910, 0301, 0360, 0685, 1101, 0501. Title matching both misses correct results and returns wrong ones.

**No credentials in source.** `GEMINI_API_KEY` and `USAJOBS_API_KEY` come from the environment, and the LLM stage degrades to keyword-only scoring when the key is absent rather than failing. The webhook URL comes from an env var or a gitignored file, format-checked before first use.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env                    # your keys
cp profile.example.json profile.json    # your profile
python job_alert.py
```

Needs a free USAJOBS developer key, a Gemini API key, and a Discord webhook. The first run seeds state and sends one summary rather than alerting on every existing posting.

## Known limitations

- **Two deployments run as forked copies rather than config profiles.** A second instance monitors a different keyword and location set, which means maintaining two divergent copies of one file. Moving source lists, weights, geography and profile into per-profile config is the next planned change.
- **The gate is tested; the rest is not.** `hard_gate` has 14 tests, including regressions for both bugs above. `job_alert.py` executes at module level, so its scoring and filter functions cannot be imported without running the whole bot. Extracting them behind a `main()` guard is the prerequisite for testing them.
- **Keyword weights were tuned by hand** against observed results, not measured against a labeled set.
- **Track calibration is unverified.** Descriptions are discarded before state is saved, so the AI/OPS distribution can't be recomputed from history — only watched run by run.
- **Exceptions are caught broadly per source** so one failing endpoint can't kill a run. The cost is that a source silently returning nothing looks much like a source that is genuinely empty.
- **Scheduling is host-dependent** (Windows Task Scheduler). A container with a cron runner would make it portable.

## What I'd do differently

Build the config layer first, and read the logs sooner. Both bugs above were sitting in output I had already generated and hadn't yet read.
