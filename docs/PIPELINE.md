# Local pipeline (Phase 4)

Double-click to scan and score automatically:

```bat
C:\Users\akhil\Auto_job_apply\start_local.bat
```

`start_local.bat` checks `.venv`, Python 3.11+, the packages and `.env`, then runs `local_launcher.py`,
which starts the **automatic scan-and-score run** (below). The pipeline (`application_pipeline.py`) only
sequences the existing Phase 1-3.4 services; it has no scoring, tailoring, state system or browser logic
of its own.

## Automatic scan and score (start_local.bat)

Double-clicking `start_local.bat` shows no menu and asks nothing. It runs exactly menu option 2
(scan and score only):

1. Takes the run lock (`output\runs\.launcher.lock`). If another launcher run holds it, it stops with
   exit code 1 and creates no folder.
2. Creates a new run folder `output\runs\<run id>\` and writes `launcher.log` there.
3. Runs the read-only launcher checks (Agent folder, `AI_AGENT_OUTPUT_DIR`, Agent Python, `artifacts.py`
   honours `artifact_sync`, no `artifact_sync` in this `.env`, packages, `.env`). A failure stops here.
4. Runs `application_pipeline.py --check-config` (read-only: the database is opened read-only, nothing
   is created, migrated or written). A failure stops here.
5. Runs `application_pipeline.py --no-apply --workflow scan-and-score --wait-for-login --run-dir <folder>`:
   session check, scan, freshness / experience / location filters, save, export at most 2, scope file,
   Agent `scrape_jobs.py` + `score_jobs.py` (scoped), import scores, eligibility report. It writes
   `run.log` and `result.json`. If nothing is exported, the Agent is not started.
6. Prints the exit code, run ID, Agent export ID and report/log paths. The window stays open when
   double-clicked.

If step 3 or 4 fails, the launcher writes `result.json` itself (`status: failed`, later steps
`NOT RUN`, counts `null`, `recorded_by: local_launcher`). If the pipeline ends without a report, the
launcher records `status: crashed` and the run is never reported as successful. Nothing is retried.

**Login.** The saved `chrome_user_data/` profile is reused. If the session check lands on Naukri's login
page, `--wait-for-login` opens the login page in the same visible browser. You log in yourself, including
any OTP or CAPTCHA; nothing is typed, stored or bypassed. The pipeline waits up to
`NAUKRI_LOGIN_WAIT_MINUTES` (default 10) once, then continues. If you don't log in, the run continues
as before (`Naukri login: AUTH_REQUIRED`, exit code 2) and the scan stops at a login page if Naukri
shows one.

The same settings as option 2 are enforced (table below), plus `AI_AGENT_STAGES=scrape_jobs.py,score_jobs.py`.
`start_local.bat` also sets them in its own process. Tailoring, Drive uploads, Sheets sync and
applications are never part of it.

## Auto-apply (opt-in)

Automatic applications are **off** unless you create `launcher_workflow.json` in the project folder
containing exactly:

```json
{"workflow": "auto-apply"}
```

Nothing else selects auto-apply: no `.env` key, no environment variable, no argument. No file means
scan and score only. A file with anything else (bad JSON, another value, extra keys) makes
`start_local.bat` stop without running anything. The file is git-ignored. Delete it to go back.

**Prerequisite (unchanged AUTO readiness gate).** AUTO mode is refused (the configuration check fails, so
the launcher stops before Chrome opens) until at least one application made in SAFE mode has been
confirmed by Naukri. Do that once yourself:
`.venv\Scripts\python.exe application_pipeline.py`, approve one job with `y` at the review, and check
it with `python main.py --action application-status`.

**What an auto-apply run does.** The launcher steps are the same (lock, run folder, read-only checks,
read-only configuration check). The pipeline then runs as
`--workflow auto-apply --wait-for-login --run-dir <folder>` with:

| Setting | Value |
|---|---|
| `APPLICATION_MODE` | `AUTO` |
| Agent stages | `scrape_jobs.py,score_jobs.py,tailor_job.py` (scoped to this run) |
| `AI_AGENT_EXPORT_LIMIT` / `APPLICATION_JOB_WINDOW_HOURS` | `2` / `24` |
| `APPLICATION_SCORE_THRESHOLD` | at least `8` (a stricter `.env` value is kept) |
| `APPLICATION_MAX_JOBS` | **your `.env` value, never raised** (must be 1-2, else the run is refused) |
| `GOOGLE_SHEETS_ENABLED` / `artifact_sync` | `false` / `0` (outcomes stay queued for a later `--sync-only`) |

Tailoring may create a Google Drive folder, a temporary Google Doc and the resume PDF upload when the
Agent has `google_drive_folder_id` (required: the launcher checks the key name). `workflow_guard` refuses
the run if any value above differs. Experience, location, freshness and history rules are unchanged.

**Which jobs.** Only jobs **exported by this run**. Before eligibility, the run's scope file is read
again and must carry this run's `run_id` and exactly the exported job ids. If it doesn't, nothing is
applied (no fallback). Recommended jobs from earlier runs are counted (`candidates_outside_run`) and left
untouched. A job that is recovery-required, already applied, or has an open attempt or an unconfirmed
record is excluded (`get_application_candidates`) and refused again by the runner's idempotency check.

**Per job** (`ApplicationRunner.process`, unchanged except for the identity check):

1. Experience gate.
2. Verified resume for this exact job.
3. Open the job page and assess it (login, CAPTCHA, closed, already applied, external).
4. **Identity check (auto-apply only).** The Naukri job id in the opened page's URL must equal the
   exported job's id. If it doesn't, or can't be read, the job becomes `requires_manual_action` /
   `JOB_IDENTITY_MISMATCH` with both ids and the URL recorded. Nothing is clicked and no other listing is
   tried.
5. The review is approved automatically.
6. Apply is marked as clicked before the click, then Naukri's own Apply is clicked.
7. Forms are submitted only when every field was planned from your `APPLICANT_*` facts. Any other
   question is left for manual completion and never guessed.
8. Only the verified job-specific resume is uploaded.
9. `applied` is recorded only on Naukri's confirmation. Anything uncertain after the click becomes
   `recovery_required` and is never retried.

**Interruptions.**

- **CAPTCHA before Apply:** a real pause. Complete it in the browser and press Enter, or `q` to stop.
  If the page is still a verification page after that, the job is not applied.
- **Login:** `--wait-for-login` handles the session check. A login page on a job stops the applications.
- **Stopping rule:** after `AUTH_REQUIRED`, `CAPTCHA_REQUIRED` or `APPLICATION_STATUS_UNKNOWN`, no further
  application is attempted in that run (`NOT_ATTEMPTED`).

**Reporting.** `result.json` adds `scope_validation`, `candidates_outside_run`, `eligibility_results`
(score, freshness, experience, location, `resume_ready` per current-run job), `applications`, and
`application_outcomes`. Each `applications` entry has the outcome (`APPLIED_CONFIRMED`,
`APPLICATION_FAILED`, `APPLICATION_SKIPPED`, `AUTH_REQUIRED`, `CAPTCHA_REQUIRED`,
`APPLICATION_STATUS_UNKNOWN`), state, code, reason, `identity_check`, resume version/type, and
`manual_completion_required`. These fields are `null` in non-auto-apply runs and when a step did not run.
The CLI's `APPLICATION_MODE=AUTO` behaviour (`mode_prompt`) is unchanged.

## Developer menu (local_launcher.py --menu)

`.venv\Scripts\python.exe local_launcher.py --menu` opens the previous interactive menu (options 1-5
below). `local_launcher.py` refuses any other argument. `start_local.bat` refuses all arguments.

**No menu option submits an application.** The menu has no apply path. Every pipeline run started
from it uses `--no-apply` and `APPLICATION_MODE=SAFE`. Independently of the launcher,
`application_pipeline.py` refuses a menu workflow (`--workflow scan-and-score|full-preparation`) unless
`--no-apply`, SAFE, Google Sheets off, `artifact_sync=0`, export cap 2, 24-hour window and scoped Agent
stages all hold (`workflow_guard`). Submitting applications is a separate, developer-CLI step (below)
that always asks for your `y`.

`start_local.bat` takes no arguments; with any argument it stops (exit code 1) without running
anything. It works from any folder and from paths with spaces. In the developer menu, Enter or
anything other than `1`-`5` selects nothing.

| Option | What it runs | Naukri / Chrome | LLM | Local writes | Google Drive | Google Sheets |
|---|---|---|---|---|---|---|
| 1 Configuration check | launcher checks + `application_pipeline.py --check-config` | no | no | **none** (database opened read-only) | no | no |
| 2 Scan and score only | `--no-apply --workflow scan-and-score`, Agent stages `scrape_jobs.py,score_jobs.py` | **yes**: live scan with the saved `chrome_user_data/` profile | **yes**: scoring, at most 2 jobs | SQLite, `exports/naukri_jobs.json`, `exports/agent_job_scope.json`, Agent `output/raw_jobs.json`, `scored_jobs.json`, `scoped_run_report.json`, `output/runs/<run id>/` | **no** | **no** |
| 3 Full preparation | `--no-apply --workflow full-preparation`, Agent stages from `AI_AGENT_STAGES` (default scrape, score, tailor) | **yes**, as option 2 | **yes**: scoring (at most 2 jobs) and tailoring for each that scores 8+ | as option 2, plus the Agent's `output/generated_resumes/`, `tailored_jobs.json` | **yes, when the Agent has `google_drive_folder_id`**: a folder per job, a temporary Google Doc to render the PDF, and the resume PDF upload | **no** |
| 4 View previous run | `application_pipeline.py --last-run` | no | no | **none** | no | no |
| 5 Exit | nothing | no | no | none | no | no |

**Confirmations.** Option 2 shows what it will do and runs only after `y` (or `yes`). Option 3 lists its
side effects (live scan, database and file updates, LLM use and possible cost, tailored resumes, Drive
uploads) and runs only when you type exactly `YES` (capitals, nothing else). Anything else cancels.

**Settings the menu enforces.** These are set in the child process environment only. `.env` is never
edited, and python-dotenv (this project and the Agent) never overrides a variable that is already set.

| Setting | Value | Why |
|---|---|---|
| `APPLICATION_MODE` | `SAFE` | never AUTO |
| `artifact_sync` | `0` | the Agent does not mirror `raw/scored/tailored_jobs.json` to Drive (`scripts/artifacts.py`). Resume uploads in option 3 are separate `tailor_job.py` calls and are not affected |
| `GOOGLE_SHEETS_ENABLED` | `false` | no Sheets sync; rows already queued in `sheet_sync` stay `PENDING` for a later, separate `--sync-only` |
| `JOB_SOURCES` | `naukri` | no LinkedIn/Apify scrape (the bridge sets it as well) |
| `AI_AGENT_EXPORT_LIMIT` | `2` | at most 2 jobs scored/tailored per run |
| `APPLICATION_JOB_WINDOW_HOURS` | `24` | the 24-hour freshness rule |
| `AI_AGENT_STAGES` | option 2: `scrape_jobs.py,score_jobs.py`; option 3: the configured value | option 3 is blocked if a configured stage cannot be scoped |

The experience rule (the master resume's verified 4+ years), current-run scope enforcement and history
preservation are not settings. They are unchanged.

**Blocked options.** At start the launcher checks, read-only: Python and packages, `.env`, that
`AI_AGENT_DIR` is the AI Agent checkout (`scripts/scrape_jobs.py`, `score_jobs.py`, `tailor_job.py`,
`job_scope.py`, `artifacts.py`), that `AI_AGENT_OUTPUT_DIR` is exactly `<AI_AGENT_DIR>\output` and
exists, the Agent's Python, that the Agent's `artifacts.py` honours `artifact_sync`, and that this
project's `.env` does **not** define `artifact_sync` (the bridge drops every key named in this `.env`
before starting the Agent, so it would cancel `artifact_sync=0`). A failure blocks options 2 and 3, or
only option 3 for unscoped `AI_AGENT_STAGES`. Options 1, 4 and 5 always work.

**One run at a time.** The automatic run and menu options 2 and 3 hold an OS lock on `output\runs\.launcher.lock` while they run.
A second launcher refuses to start a run. The OS releases the lock when the process ends, even after a
crash. The empty lock file stays and is harmless. Direct CLI runs do not take this lock.

**After each run** the launcher shows the process's real exit code and its meaning, the run ID, the
Agent export ID, the report and log paths, and `RUN COMPLETE` only for exit code 0. A missing or
unfinished report, or a report that disagrees with the exit code, is called out. Nothing is retried.

### Run logs and reports

The automatic run and menu options 2 and 3 record each run in a new folder `output\runs\<YYYYmmddTHHMMSS>-<random>\` (git-ignored).
Folders are created atomically and never reused, and older runs are never deleted.

- `run.log`: everything printed to the console during the run (progress, log records, warnings,
  tracebacks), including the AI Agent stages' own output and errors (UTF-8).
- `result.json`: written with `"status": "running"` when the run starts and replaced atomically when it
  ends. A run that was killed keeps an honest, incomplete report.

| `result.json` field | Content |
|---|---|
| `run_id`, `started_at`, `finished_at`, `duration_seconds` | run folder name; local time with UTC offset |
| `status` | `running` (never finished), `complete`, `attention`, `failed`, `interrupted`, `crashed` |
| `workflow`, `application_mode`, `no_apply`, `options` | `scan-and-score` / `full-preparation` / `cli`; the pipeline flags |
| `effective_settings` | stages, export cap, window, threshold, Sheets on/off, `artifact_sync` (no secrets) |
| `exit_code`, `exit_meaning` | `0` complete, `1` configuration invalid or unrecoverable error, `2` needs attention |
| `stages` | per step: configuration, Naukri session, scan, export, AI Agent, applications, Sheets (`NOT RUN` when a step never started) |
| `agent_export_id`, `exported_job_ids` | the scope file's `run_id` and the `jobs.id`s exported this run |
| `agent_scope_report` | the Agent's own report for this run, when it wrote one |
| `counts` | the summary counts. **`null` = unknown** (step not run, crashed run), never a fake 0 |
| `warnings`, `fatal`, `error`, `summary_text`, `log_file`, `report_file` | as printed |

`--last-run` (menu option 4, or `.venv\Scripts\python.exe application_pipeline.py --last-run`) shows the newest report under `output\runs\`. A newer folder without a readable
report is named and skipped, and an unfinished report is shown with a warning. It only reads: no
database, browser, network, new folder or file.

### Developer CLI (no menu)

`application_pipeline.py` can still be run directly. Its flags are unchanged, apart from
`--check-config`, which is now read-only. Use the project's interpreter:

```bat
.venv\Scripts\python.exe application_pipeline.py --check-config      :: read-only configuration check
.venv\Scripts\python.exe application_pipeline.py --last-run          :: newest run report (read-only)
.venv\Scripts\python.exe application_pipeline.py --dry-run           :: open and check jobs; never click Apply, export, run the Agent or write Sheets
.venv\Scripts\python.exe application_pipeline.py --no-apply          :: scan + score + eligibility only
.venv\Scripts\python.exe application_pipeline.py                     :: full run INCLUDING applications (SAFE: each needs your y)
.venv\Scripts\python.exe application_pipeline.py --sync-only         :: retry queued Google Sheets rows
```

`--run-dir DIR` records any CLI run like the menu does (DIR must not already hold `run.log` or
`result.json`). These commands use `.env` as it is. The menu's enforced settings do **not** apply, so
Sheets sync and the Agent's Drive mirroring follow `.env` and the Agent's own configuration.

## Architecture

```
start_local.bat → local_launcher.py              (menu; preparation only, --no-apply)
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
  stays unscored and is offered again on a later run while still fresh) or `REJECTED`
  (e.g. description under 80 characters), with its rank in brackets.
- **Export order** (`export_priority.py`, pipeline only). The cap's slots go first to the target roles
  by title tier: Java with Spring/Spring Boot/Backend/Full Stack, then Java Developer, then Full Stack,
  then generic developer titles with Java skills. Testing/QA titles and titles naming another stack
  rank last. Within a tier: more distinct matching skills (Java, Spring Boot, Spring, Microservices,
  REST, Hibernate, JPA, J2EE, Angular, React, SQL, Kafka, JUnit; "JavaScript" is never Java), then the
  older posting (closer to leaving the 24-hour window; unknown age last), then `jobs.id`. Ranking only
  orders jobs that already passed every check; it never makes a job eligible.
  `main.py --action export-to-agent` keeps the plain `jobs.id` order.
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

`start_local.bat` is the automatic scan and score (no arguments); `local_launcher.py --menu` is the
developer menu. Direct pipeline commands (developer CLI, run from
the project folder):

```bat
.venv\Scripts\python.exe application_pipeline.py                  :: full run (SAFE unless APPLICATION_MODE=AUTO)
.venv\Scripts\python.exe application_pipeline.py --dry-run        :: open and check jobs; never click Apply, export, run the Agent or write Sheets
.venv\Scripts\python.exe application_pipeline.py --no-apply       :: scan + score + eligibility only (no application browser)
.venv\Scripts\python.exe application_pipeline.py --skip-scan      :: use the jobs already in SQLite
.venv\Scripts\python.exe application_pipeline.py --skip-agent     :: do not run the AI Agent (scores already produced are still imported)
.venv\Scripts\python.exe application_pipeline.py --sync-only      :: only retry pending Google Sheets rows
.venv\Scripts\python.exe application_pipeline.py --check-config   :: read-only configuration check
.venv\Scripts\python.exe application_pipeline.py --last-run       :: newest run report (read-only)
```

Exit codes: `0` run complete, `1` setup/configuration problem or unrecoverable error, `2` finished but
something needs you (login, recovery, failure, AI Agent error, stopped run).

`start_local.bat` never installs packages, never touches `chrome_user_data/`, never edits `.env` and
never resets the Naukri login. When double-clicked it keeps the window open to read the summary.

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

1. `.venv\Scripts\python.exe application_pipeline.py --dry-run` and check the summary and the jobs it
   would apply to.
2. `.venv\Scripts\python.exe application_pipeline.py` in SAFE mode, approve exactly **one** job, and confirm it on Naukri, in
   `python main.py --action application-status --job-id <id>` and in the Sheet.
3. Only then consider `APPLICATION_MODE=AUTO` (the pipeline enforces step 2).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `[FAIL] .venv not found` / missing packages | create the venv and install `requirements.txt` once (the launcher never installs) |
| Launcher: `Option 2/3 is blocked` | run option 1; fix each `FAIL` line (Agent paths, artifacts.py, `artifact_sync` in this `.env`, stages) |
| Launcher: `Another launcher run is in progress` | another launcher is running option 2 or 3; wait for it (a crashed one releases the lock automatically) |
| `workflow ... refused` | a menu workflow was started without its safety settings; use `start_local.bat`, or run the CLI without `--workflow` |
| `Naukri login: AUTH_REQUIRED` | `python main.py --action naukri-login`, log in by hand, rerun |
| `BROWSER_PROFILE_LOCKED` | close other Chromium windows using `chrome_user_data/` |
| `AI Agent: AGENT_NOT_CONFIGURED` | set `AI_AGENT_OUTPUT_DIR` in `.env` |
| `AI Agent: AGENT_STAGE_FAILED` | run the named script in the Agent folder to see its log (e.g. Ollama not running in LOCAL_MODE) |
| `Eligible: 0` | `python main.py --action application-candidates` shows why (freshness, score, resume, history) |
| Sheets `AUTH_REQUIRED` / `GWS_NOT_INSTALLED` | `gws auth login -s drive,docs,sheets`, then `--sync-only` |
| Sheets `SHEET_NOT_CONFIGURED` | set `GOOGLE_SHEET_ID` or `google_sheet_id` in the Agent's `.env` |
| `Recovery required: N` | `python main.py --action application-status`, then `recover-application --job-id <id>` |
| `AUTO mode readiness` failure | apply to one job in SAFE mode first |
