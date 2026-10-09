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
`found_at` / `first_seen_at` are never used as freshness.

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
