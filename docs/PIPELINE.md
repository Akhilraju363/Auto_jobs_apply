# Local pipeline (Phase 4)

One command runs the whole Naukri workflow on this machine:

```bat
C:\Users\akhil\Auto_job_apply\start_local.bat
```

`start_local.bat` validates the setup and runs `python application_pipeline.py`. The pipeline only
sequences the existing Phase 1-3.4 services; it has no scoring, tailoring, state system or browser
logic of its own.

## Architecture

```
start_local.bat
  └─ application_pipeline.py                     (orchestrator)
       1  validate configuration                  validate_configuration()
       2  verify Naukri session                   NaukriApplicationBrowser.session_state()  (read-only)
       3  scan Naukri                             naukri_scanner.NaukriScanner
       4  keep FRESH jobs (24h)                   eligibility.job_freshness
       5  drop already-applied jobs               db.has_successful_application
     5b  experience check (before any LLM call)  experience_check.check_experience      -> INELIGIBLE / REVIEW_REQUIRED never exported
       6  export to the AI Agent                  ai_agent_bridge.export_discovered_jobs  -> exports/naukri_jobs.json
       7  run the AI Agent                        ai_agent_bridge.run_agent_stages        (its own scripts, own venv)
       8  import scores                           ai_agent_bridge.import_agent_scores     <- output/scored_jobs.json
       9  eligibility                             db.get_application_candidates + eligibility.evaluate_job
      10  verified resume                         resume_artifact.resolve_verified_resume
   11-13  prepare / approve / apply / verify      naukri_application.ApplicationRunner    (unchanged Phase 3.3 rules)
      14  persist                                 SQLite (written by the runner as it goes)
   15-16  Google Sheets sync + retry pending      sheets_tracker.sync_pending_outcomes
      17  summary
```

## SQLite

`data/naukri_auto_apply.db` (override with `NAUKRI_DB_PATH`). No database server; the file persists
between runs. On the first run the pre-Phase-4 `jobs.db` is copied there once with SQLite's backup
API; `jobs.db` itself is left untouched as a backup and is no longer read.

| Table | Purpose |
|---|---|
| `jobs` | every discovered job: Naukri data, AI score, freshness, `application_state` / `application_code` |
| `applied_jobs` | application history; a row with `status='applied'` is a confirmed application and is never rewritten |
| `application_attempts` | write-ahead attempt log; `apply_clicked` is set *before* Apply is clicked |
| `sheet_sync` | Google Sheets outbox: one row per job whose outcome must reach the tracker (`PENDING` / `SYNCED`) |

## Duplicate applications

**Successfully applied job = never automatically resubmitted.**
**Same company ≠ same job.**

A job counts as already applied only when `applied_jobs` holds a confirmed application for that exact
job, matched by (strongest first) its applied_jobs key, its canonical job URL (https, no query string),
its Naukri job ID, or its `dedup_key`. Company and title are never used: if you applied to Infosys
"Java Full Stack Developer" (job A) yesterday, Infosys "Java Developer" (job B) today is still eligible.

Protection holds across restarts, browser crashes, network failures, Sheets failures and interrupted
runs because it lives in SQLite and is checked before every application:

- confirmed applied (`applied_jobs`) → skipped (`already_applied`)
- Naukri shows the job as Applied → skipped and recorded
- an attempt with `apply_clicked` but no confirmation → `recovery_required`, never retried
- `recovery_required` → blocked until `python main.py --action recover-application --job-id <id>`

## The 24-hour rule

Only jobs posted/updated within `APPLICATION_JOB_WINDOW_HOURS` (default 24) are exported or applied to.
Exact timestamps win. A relative label such as "5 hours ago" describes the moment of the scan, so it is
aged by the time since `scraped_at`: a job read as "Today" three days ago is STALE now (before Phase 4
such a job stayed FRESH forever). Ambiguous labels such as "1 day ago" are UNKNOWN, never fresh.
`found_at` / `first_seen_at` are never used as freshness. "N weeks/months ago" and "30+ days ago"
are STALE. An exact, timezone-aware JSON-LD `datePosted` is recorded and used only to settle a label
that is ambiguous on its own ("1 day ago"); it never overrides a clear label (a reposted job keeps its
"Just now").

## Discovery

The scanner searches each keyword (`NAUKRI_KEYWORDS`, Java roles first) in the preferred cities
(`NAUKRI_LOCATIONS`, default Bengaluru then Chennai, then `NAUKRI_LOCATION` and `PREFERRED_LOCATIONS`),
then once without a location (`NAUKRI_SEARCH_ALL_LOCATIONS`), so the per-scan job budget
(`NAUKRI_MAX_JOBS`) fills in that priority. Searches use Naukri's `jobAge` filter
(`NAUKRI_JOB_AGE_DAYS`, default 1) and stop after `NAUKRI_MAX_SEARCH_PAGES` result pages in total.

Before a job page is opened, its search card is checked: a label already older than the window, or a
Naukri experience minimum above the resume's verified years, is skipped and does not use the budget.
Ambiguous freshness, missing or conflicting experience are opened and decided on the full job; the
full-job experience check is stored when the job is saved.

A job page whose description is empty is read again after waiting for the description element, then
once more after one reload. If it is still empty the job is not stored (the next scan retries it) and
the run reports it; a stored job with an empty description is `REVIEW_REQUIRED`, never eligible.

## Experience eligibility

The master ATS resume (`MASTER_RESUME_PATH`, default the AI Agent's `resume/base_resume.md`) is the only
source of your experience. Its explicit claim ("4+ years of experience") is read as its floor, 4 years,
and is never rounded up; `APPLICANT_TOTAL_EXPERIENCE` can only lower it.

A job's requirement is Naukri's experience field ("5 - 8 years", authoritative) plus explicit mentions in
the title and description ("3+ years of experience", "4 to 6 years", "Minimum 4 years"). Ranges are
compared by their **minimum**.

| Result | When | Effect |
|---|---|---|
| `ELIGIBLE` | every stated minimum <= verified years | the other checks continue |
| `INELIGIBLE` | Naukri's minimum (or every stated minimum) > verified years | skipped: `Skipped: JD requires 5–8 years; resume supports 4+ years.` |
| `REVIEW_REQUIRED` | no requirement found, sources conflict, or the resume states no experience | never applied |

The check runs before export, so failing jobs cost no AI scoring/tailoring calls, and again at
eligibility. `ApplicationRunner.process` enforces it once more on its own, whoever calls it: after the
duplicate/recovery check and before the job page, resume, attempt log or Apply control is touched, it
checks both the passed job and its stored row (the stricter wins). A refused job gets
`application_state=not_ready` with `application_code` `EXPERIENCE_INELIGIBLE` or
`EXPERIENCE_REVIEW_REQUIRED` (reported as SKIPPED); `applied_jobs` and final/recovery states are never
touched, and any error in the check fails closed. The legacy `main.py --mode cli` drivers run the same
check before clicking Apply. Results are stored on `jobs` (`experience_required`, `experience_required_min`,
`experience_verified`, `experience_verified_years`, `experience_check_status`, `experience_check_reason`,
`experience_checked_at`). Report without browsing, exporting, running the Agent, writing Sheets or applying:

```bat
python application_pipeline.py --eligibility-check
```

## Reading the run summary

Every count is about **this run** unless it says "all runs".

- **Fresh stored jobs**: every stored job inside the window, scored or not.
- **Export to AI Agent (this run)**, unscored jobs only: not fresh -> never exported; experience
  INELIGIBLE / REVIEW_REQUIRED -> never exported (reason printed per job); the rest are "fresh,
  experience-eligible" and each prints one line: `EXPORTED`, `DEFERRED` (past `AI_AGENT_EXPORT_LIMIT`;
  oldest first; stays unscored and is offered again on a later run while still fresh) or `REJECTED`
  (e.g. description under 80 characters).
- **AI Agent (this run's exported jobs only)**: from the Agent's report for this run
  (`output/scoped_run_report.json`, matched by run id): newly scored, newly tailored, already processed
  (skipped), below the 8+ cutoff, not in the Agent store, failed/flagged. "unknown" means the Agent
  wrote no report for this run -- never read as 0. "Other jobs the Agent processed" compares its output
  files before and after the run and should be 0 in a scoped run; anything else is a warning. The
  Agent's own console counts (e.g. "68 already scored", "12 qualified") are its cumulative history.
- **AI scoring (imported into SQLite)**: "This run's exports scored / recommended" are this run's
  results. "All scores imported" can also include earlier exports the Agent scored outside this run.
- **Eligibility**: "Recommended, not applied (all runs)" is every AI-recommended job in history;
  "Eligible to apply now" also passed freshness, experience, score and resume checks today.
- **Google Sheets**: only application outcomes (apply / recover) are queued. `UP_TO_DATE` means
  nothing was pending (normal for `--no-apply`); `SYNCED` means rows were written ("Rows written").

## SAFE and AUTO

| | SAFE (default) | AUTO |
|---|---|---|
| Review approval | you type exactly `y` or `yes`; anything else (Enter, `n`, `a`, "approve all") is no | given automatically to jobs that passed every eligibility rule |
| Questions, uploads, CAPTCHA, manual steps | you | declined: the job goes to manual action / recovery, nothing is guessed |
| Enabled by | nothing | `APPLICATION_MODE=AUTO` **and** at least one confirmed application in history |

AUTO is refused (configuration error, nothing runs) until one SAFE-mode application has been
confirmed end to end. An unknown `APPLICATION_MODE` value is an error, never AUTO. There is no
"approve all". At most `APPLICATION_MAX_JOBS` applications are attempted per run.

## AI Agent dependency

The AI Agent (`job-application-ai-agent`) stays a separate project; the integration is still the file
handoff. The pipeline runs the Agent's own stage scripts (`AI_AGENT_STAGES`, default
`scrape_jobs.py,score_jobs.py,tailor_job.py`) with the Agent's interpreter in the Agent's folder, with:

- `NAUKRI_JOBS_PATH` = this project's `NAUKRI_EXPORT_PATH`
- `JOB_SOURCES=naukri` (the pipeline never triggers a paid LinkedIn/Apify scrape)
- every key from **this** project's `.env` removed, so the Agent uses its own `.env` (python-dotenv
  never overrides existing variables, so e.g. our `GEMINI_API_KEY` would otherwise leak into it)

**Scoped runs.** The Agent may score and tailor only the jobs this run exported. Next to the export the
pipeline writes `agent_job_scope.json` (`{"run_id", "jobs": [{"id": <Naukri job id>, "link"}]}`) and
passes it to the Agent's `score_jobs.py` and `tailor_job.py` as `--job-ids-file` (Agent:
`scripts/job_scope.py`, see its `docs/RUNBOOK.md`). The Agent's cached LinkedIn jobs and older jobs are
not scored or tailored in these runs, and its history is not rewritten. The run is refused before any
stage starts -- never retried unscoped -- if the Agent has no `scripts/job_scope.py`, if
`AI_AGENT_STAGES` contains a stage other than `scrape_jobs.py`, `score_jobs.py`, `tailor_job.py`, or if
the scope file is missing. Nothing exported -> the Agent is not run at all.

**Cost control.** `AI_AGENT_EXPORT_LIMIT` (default 2; not a whole number -> 2, below 1 -> 1, shown by
`--check-config`) caps the jobs sent per run; each can cost a scoring and a tailoring LLM call. A low cap
keeps LLM use small but fresh jobs can pass the 24-hour window while deferred, and then they are never
scored; a higher cap scores more of them at more LLM cost. Nothing is ever sent beyond the cap.

Only fresh, never-applied, unscored jobs are exported (up to `AI_AGENT_EXPORT_LIMIT`). Scores keep the
Phase 2 mapping (1-10: 8+ recommended, 6-7 review, else skipped) and applications need
`APPLICATION_SCORE_THRESHOLD` (default 8). If the Agent is not configured or fails, the run continues
with jobs scored earlier and reports the error.

## Google Sheets

The tracker is the AI Agent's "Job Application Tracker" sheet, written the way its
`scripts/write_sheet.py` writes it: through the `gws` CLI, whose own stored login is reused (this
project stores no Google credentials and never asks you to re-authenticate). Sheet id:
`GOOGLE_SHEET_ID`, else `google_sheet_id` from the AI Agent's `.env` (only that key is read).

One row per job (canonical link, then Naukri job ID). If the Agent already added the job when it
tailored the resume, that row is updated instead of adding a second one. Columns A:L are the Agent's
(unchanged); M:S are added for Naukri:

| Column | Content |
|---|---|
| A-E | Job Title, Company, Job Link, Fit Score (1-10), Resume Path |
| F Status | `Applied` once confirmed, else `Not Applied`; a status you set (Interviewing, Offer, Rejected) is never overwritten |
| G Timestamp, I Source, J Status Updated | first logged date, `Naukri`, last update date |
| M Application State | APPLIED, NOT_APPROVED, READY, APPLYING, FAILED, RECOVERY_REQUIRED, SKIPPED |
| N-S | Applied At, Naukri Job ID, Location, Experience, AI Recommendation, Failure Reason |

### Google Sheets failure never causes reapplication

```
Naukri confirms  ->  SQLite: APPLIED (applied_jobs)  ->  sheet_sync: PENDING  ->  Sheets write  ->  SYNCED
```

The application is recorded in SQLite before Sheets is touched. If Sheets fails (`AUTH_REQUIRED`,
`GWS_NOT_INSTALLED`, network) the row stays `PENDING`, the job stays APPLIED, and the next run retries
the sync — it never reapplies. Retry by hand: `python application_pipeline.py --sync-only`.

If `gws` is not logged in the summary shows `AUTH_REQUIRED` with the pending count; log in once with
`gws auth login -s drive,docs,sheets` (see the AI Agent's GWS_SETUP.md).

## Recovery

Unchanged from Phase 3.3 and read-only: `python main.py --action application-status` lists jobs that
need attention; `python main.py --action recover-application --job-id <id>` opens the job on Naukri
without clicking anything and settles it with you. The pipeline never resubmits an uncertain job.

## Commands

```bat
start_local.bat                     :: full run (SAFE unless APPLICATION_MODE=AUTO)
start_local.bat --dry-run           :: open and check jobs; never click Apply, export, run the Agent or write Sheets
start_local.bat --no-apply          :: scan + score + eligibility only (no application browser)
start_local.bat --skip-scan         :: use the jobs already in SQLite
start_local.bat --skip-agent        :: do not run the AI Agent (scores already produced are still imported)
start_local.bat --sync-only         :: only retry pending Google Sheets rows
python application_pipeline.py --check-config
```

Exit codes: `0` run complete, `1` setup/configuration problem (nothing applied), `2` finished but
something needs you (login, recovery, failure, AI Agent error, stopped run).

`start_local.bat` never installs packages, never touches `chrome_user_data/` and never resets the
Naukri login. When double-clicked it keeps the window open to read the summary.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `APPLICATION_MODE` | `SAFE` | `SAFE` or `AUTO` (see above) |
| `APPLICATION_JOB_WINDOW_HOURS` | `24` | freshness window |
| `APPLICATION_SCORE_THRESHOLD` | `8` | minimum AI Agent score (1-10) |
| `APPLICATION_MAX_JOBS` | `1` | applications per run |
| `AI_AGENT_OUTPUT_DIR` | — | the Agent's `output/` folder (**required for scoring**) |
| `AI_AGENT_DIR` | parent of `AI_AGENT_OUTPUT_DIR` | the Agent checkout |
| `AI_AGENT_PYTHON` | `<AI_AGENT_DIR>\.venv\Scripts\python.exe` | the Agent's interpreter |
| `AI_AGENT_STAGES` | `scrape_jobs.py,score_jobs.py,tailor_job.py` | Agent scripts to run |
| `AI_AGENT_TIMEOUT_MINUTES` | `60` | per stage |
| `AI_AGENT_EXPORT_LIMIT` | `2` | jobs exported per run |
| `NAUKRI_EXPORT_PATH` | `exports/naukri_jobs.json` | passed to the Agent as `NAUKRI_JOBS_PATH` |
| `GOOGLE_SHEETS_ENABLED` | `true` | `false` keeps outcomes queued locally |
| `GOOGLE_SHEET_ID` | Agent's `google_sheet_id` | tracker sheet |
| `GOOGLE_SHEET_TAB` | `Sheet1` | tracker tab |
| `NAUKRI_DB_PATH` | `data/naukri_auto_apply.db` | SQLite file |

## Before trusting AUTO

1. `start_local.bat --dry-run` and check the summary and the jobs it would apply to.
2. `start_local.bat` in SAFE mode, approve exactly **one** job, and confirm it on Naukri, in
   `python main.py --action application-status --job-id <id>` and in the Sheet.
3. Only then consider `APPLICATION_MODE=AUTO` (the pipeline enforces step 2).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[FAIL] .venv not found` / missing packages | create the venv and install `requirements.txt` once (the launcher never installs) |
| `Naukri login: AUTH_REQUIRED` | `python main.py --action naukri-login`, log in by hand, rerun |
| `BROWSER_PROFILE_LOCKED` | close other Chromium windows using `chrome_user_data/` |
| `AI Agent: AGENT_NOT_CONFIGURED` | set `AI_AGENT_OUTPUT_DIR` in `.env` |
| `AI Agent: AGENT_STAGE_FAILED` | run the named script in the Agent folder to see its log (e.g. Ollama not running in LOCAL_MODE) |
| `Eligible: 0` | `python main.py --action application-candidates` shows why (freshness, score, resume, history) |
| Sheets `AUTH_REQUIRED` / `GWS_NOT_INSTALLED` | `gws auth login -s drive,docs,sheets`, then `--sync-only` |
| Sheets `SHEET_NOT_CONFIGURED` | set `GOOGLE_SHEET_ID` or `google_sheet_id` in the Agent's `.env` |
| `Recovery required: N` | `python main.py --action application-status`, then `recover-application --job-id <id>` |
| `AUTO mode readiness` failure | apply to one job in SAFE mode first |
