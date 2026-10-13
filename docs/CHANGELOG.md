# Changelog

Newest first. Entries are uncommitted local work unless a commit is named.

- 2026-10-13 Relevance-first export (`export_priority.py`): the pipeline fills the 2 export slots by role tier
  (Java + Spring/Backend/Full Stack > Java Developer > Full Stack > generic dev with Java skills > other),
  then distinct matching skills, then expiry risk, then `jobs.id` (`export_discovered_jobs(order_key=...)`,
  opt-in; `main.py --action export-to-agent` keeps `jobs.id` order). Run 20261013T103118-03c417 replayed:
  418 and 442 (Java Full Stack) instead of 389 (Junior Developer) and 390 (API Testing). Eligibility unchanged.
  Resume-not-ready reasons now include the resolver's code and reason.

- 2026-10-13 Opt-in auto-apply: only `launcher_workflow.json` = `{"workflow": "auto-apply"}` (git-ignored,
  never created for you) makes `start_local.bat` run `--workflow auto-apply` (AUTO, scrape/score/tailor,
  export cap 2, 24h, score >= 8, `APPLICATION_MAX_JOBS` from `.env` (1-2), Sheets and artifact sync off). The
  AUTO readiness gate is unchanged. Candidates come only from this run's re-validated scope file; the runner
  checks the Naukri job id of the opened page (`JOB_IDENTITY_MISMATCH`); `auto_apply_prompt` approves the
  review and fact-only forms, pauses for CAPTCHA, leaves everything else for manual completion; per-job
  outcomes (`APPLIED_CONFIRMED` ... `APPLICATION_STATUS_UNKNOWN`) in `result.json`. Absent file: unchanged.

- 2026-10-13 One-click scan: `start_local.bat` now runs the automatic scan and score (`local_launcher.AutoRun`,
  menu option 2 without menu or confirmation): lock → run folder + `launcher.log` → read-only launcher checks
  → read-only `--check-config` → pipeline `--no-apply --workflow scan-and-score --wait-for-login`. Failures
  before the pipeline, or a pipeline without a report, are recorded in `result.json` by the launcher. New
  pipeline flag `--wait-for-login`: on a login page, wait once (`NAUKRI_LOGIN_WAIT_MINUTES`) for a manual
  login in the visible browser. The interactive menu moved to `local_launcher.py --menu`. Agent stage output
  in `run.log` no longer doubles Windows line endings.

- 2026-10-13 Menu launcher: `start_local.bat` is menu-only (`local_launcher.py`): configuration check,
  scan and score, full preparation (`YES`), last run, exit. No menu path submits: `--no-apply`, SAFE,
  `artifact_sync=0`, `GOOGLE_SHEETS_ENABLED=false`, export cap 2 and 24h are enforced through the process
  environment, and `application_pipeline.workflow_guard` refuses a menu workflow without them. New
  `--run-dir` (run.log + result.json, unknown counts are null) and read-only `--last-run`;
  `--check-config` no longer calls `init_db()` (read-only `db.inspect_database_readonly()`). The Agent
  export ID is kept on `RunSummary` and shown in the summary. Arguments to `start_local.bat` are refused.

- 2026-10-08 Phase 4: local pipeline (`application_pipeline.py`, `start_local.bat`). SQLite moves to
  `data/naukri_auto_apply.db` (one-time copy of `jobs.db`, which is left untouched). Application history
  now matches the canonical job URL, Naukri job ID and dedup_key (never company). `sheet_sync` outbox +
  `sheets_tracker.py` write outcomes to the AI Agent's tracker sheet via `gws` (columns A:L unchanged,
  M:S added); Sheets failures stay pending and never cause reapplication. `ai_agent_bridge` can filter
  the export and run the Agent's own stage scripts (own venv, own `.env`, `JOB_SOURCES=naukri`).
  `APPLICATION_MODE` SAFE (default) / AUTO (refused until one confirmed application). Fix: relative
  posted labels are now aged from `scraped_at`, so a job read as "Just now" yesterday is no longer FRESH.

- 2026-10-07 Phase 3.4: deterministic 24-hour freshness statuses (`FRESH`, `STALE`, `UNKNOWN`),
  explicit preferred-location and experience eligibility, score/resume/history protection,
  display-only prioritization, read-only `fresh-jobs`, and expanded candidate output.
- 2026-10-07 Phase 3.3: outcome tracking and recovery. `application_outcome.py` (deterministic
  confirmation, idempotency), `application_audit.py` (events, sanitized diagnostics), `application_attempts`
  write-ahead history, `recovery_required` state, `application-status` and `recover-application`,
  navigation-only retries, delayed-confirmation wait. Fix: Naukri's `<span id="already-applied">` is now
  detected (it was missed because only buttons/links were read).
- 2026-10-07 Phase 3.2: verified PDF resume handoff. `resume_artifact.py` resolves the AI Agent's verified,
  job-specific `generated_resumes/<id>/v<n>.pdf` (identity, verification, md_sha1, parse, text consistency
  via pypdf). Naukri upload-control resolver + confirmation, file questions in the chat flow,
  `--action verify-resume`; dry-run shows the upload candidate. ApplicationStop now records its real code.
- 2026-10-07 Phase 3.1: deterministic application-question engine (`application_profile.py`,
  `application_questions.py`). Configured facts and true/false preferences are answered
  automatically in Naukri's chat questions (one at a time, Save, next). "Do you have experience
  with X" is answered Yes only from the master resume. Free text, legal, sensitive, unknown and
  unconfigured questions go to the human with a re-check before continuing. Values are never
  logged. `APPLICANT_ANSWERS` was replaced by `ApplicationProfile`. New `MASTER_RESUME_PATH`.
- 2026-10-07 Phase 3 hotfix: the Apply-control resolver picks the job-header `#apply-button` when
  Naukri also renders a sticky-header copy; ambiguous pages still fail closed.
- 2026-10-07 Phase 3: human-approved Naukri application preparation (`naukri_application.py`,
  `--action application-candidates|naukri-login|verify-job|apply`, application state and history,
  `jobs.db` untracked).
- 2026-10-07 Phase 2: AI Agent file handoff (`ai_agent_bridge.py`, `export-to-agent`, `import-scores`).
- 2026-10-07 Pre-Phase-2 hardening: `.gitignore`, scoring columns, reproducible requirements.
- 2026-10-07 Phase 1: Naukri job scanner (`naukri_scanner.py`, `naukri_parser.py`, `jobs` table).
