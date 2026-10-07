# Changelog

Newest first. Entries are uncommitted local work unless a commit is named.

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
