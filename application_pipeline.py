#!/usr/bin/env python3
"""Phase 4 local orchestrator: one run of the whole Naukri workflow (start_local.bat runs this).

    configuration -> Naukri session -> scan -> fresh (24h) jobs -> drop already-applied ->
    export to the AI Agent -> AI Agent stages -> import scores -> eligibility -> resume ->
    prepare / approve / apply -> verify -> SQLite -> Google Sheets (+ retry pending) -> summary

This module only sequences the existing services; it adds no scoring, no tailoring, no state
system and no browser logic of its own:
- discovery:      naukri_scanner.NaukriScanner
- AI Agent:       ai_agent_bridge (file handoff + the Agent's own stage scripts as subprocesses)
- eligibility:    eligibility.evaluate_job + db.get_application_candidates (never bypassed)
- applying:       naukri_application.ApplicationRunner (idempotency, write-ahead apply_clicked,
                  confirmation, recovery_required -- unchanged)
- history:        db (SQLite, the source of truth for duplicate prevention)
- tracking:       sheets_tracker (reporting only; failures stay pending, never cause a reapply)

APPLICATION_MODE=SAFE (default): every submission waits for your exact 'y' / 'yes'.
APPLICATION_MODE=AUTO: the review approval is given automatically for jobs that passed every
eligibility rule; every other question still goes to a human, and AUTO is refused until at least
one SAFE-mode application has been confirmed.
"""

import argparse
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import traceback
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from config import (
    AI_AGENT_DIR,
    AI_AGENT_EXPORT_LIMIT,
    AI_AGENT_EXPORT_LIMIT_SETTING,
    AI_AGENT_OUTPUT_DIR,
    AI_AGENT_PYTHON,
    AI_AGENT_STAGES,
    AI_AGENT_TIMEOUT_MINUTES,
    APPLICATION_JOB_WINDOW_HOURS,
    APPLICATION_MAX_JOBS,
    APPLICATION_MODE,
    APPLICATION_SCORE_THRESHOLD,
    BASE_DIR,
    CHROME_USER_DATA,
    DB_PATH,
    GOOGLE_SHEET_ID,
    GOOGLE_SHEET_TAB,
    GOOGLE_SHEETS_ENABLED,
    MASTER_RESUME_PATH,
    NAUKRI_ENABLED,
    NAUKRI_EXPORT_PATH,
    NAUKRI_LOGIN_WAIT_MINUTES,
    PREFERRED_LOCATIONS,
)
from ai_agent_bridge import scope_id as bridge_scope_id
from ai_agent_bridge import (
    SCOPE_SAFE_STAGES,
    AgentRunResult,
    agent_canonical_link,
    read_scope_report,
    export_discovered_jobs,
    import_agent_scores,
    resolve_agent_python,
    run_agent_stages,
)
from application_outcome import (
    OUTCOME_APPLIED,
    OUTCOME_FAILED,
    OUTCOME_RECOVERY_REQUIRED,
    application_outcome_label,
)
from db import (
    FINAL_APPLICATION_STATES,
    JOB_STATUS_DISCOVERED,
    count_successful_applications,
    get_application_candidates,
    get_job,
    get_jobs,
    has_successful_application,
    init_db,
    inspect_database_readonly,
    queue_sheet_sync,
    save_experience_check,
)
from eligibility import FRESH, RESUME_NOT_READY_REASON, Eligibility, evaluate_job, job_freshness, priority_score
from export_priority import priority as export_priority
from export_priority import relevance_order

EXPORT_ORDER_LABEL = "ranked by role relevance, then matching skills, then expiry risk"
from experience_check import (
    ELIGIBLE,
    INELIGIBLE,
    REVIEW_REQUIRED,
    VerifiedExperience,
    record_experience_check,
    verified_resume_experience,
)
from resume_artifact import resolve_verified_resume
from sheets_tracker import (
    DISABLED,
    SHEET_NOT_CONFIGURED,
    GwsClient,
    SheetTracker,
    SyncReport,
    resolve_gws_command,
    resolve_sheet_id,
    sync_pending_outcomes,
)

logger = logging.getLogger(__name__)

SAFE, AUTO = "SAFE", "AUTO"
MODES = (SAFE, AUTO)
APPROVAL_PROMPT = "Approve this application?"  # ApplicationRunner's review question

EXIT_OK = 0  # run complete
EXIT_CONFIG = 1  # configuration invalid or unrecoverable error: nothing was applied
EXIT_ATTENTION = 2  # run finished but something needs you (login, recovery, failure, AI Agent error)


# ---------------------------------------------------------------------------
# Settings and configuration validation
# ---------------------------------------------------------------------------


@dataclass
class PipelineSettings:
    mode: str = APPLICATION_MODE
    window_hours: int = APPLICATION_JOB_WINDOW_HOURS
    score_threshold: float = APPLICATION_SCORE_THRESHOLD
    preferred_locations: list = field(default_factory=lambda: list(PREFERRED_LOCATIONS))
    max_applications: int = APPLICATION_MAX_JOBS
    export_limit: int = AI_AGENT_EXPORT_LIMIT
    export_path: Path = NAUKRI_EXPORT_PATH
    agent_dir: Optional[Path] = AI_AGENT_DIR
    agent_output_dir: Optional[Path] = AI_AGENT_OUTPUT_DIR
    agent_python: Optional[Path] = AI_AGENT_PYTHON
    agent_stages: list = field(default_factory=lambda: list(AI_AGENT_STAGES))
    agent_timeout_minutes: int = AI_AGENT_TIMEOUT_MINUTES
    sheets_enabled: bool = GOOGLE_SHEETS_ENABLED
    sheet_id: Optional[str] = GOOGLE_SHEET_ID or None
    sheet_tab: str = GOOGLE_SHEET_TAB
    chrome_profile: Path = CHROME_USER_DATA
    naukri_enabled: bool = NAUKRI_ENABLED
    master_resume_path: Optional[Path] = MASTER_RESUME_PATH  # source of truth for verified experience

    def resolved_sheet_id(self) -> Optional[str]:
        return resolve_sheet_id(self.sheet_id, self.agent_dir)


@dataclass(frozen=True)
class ConfigCheck:
    name: str
    ok: bool
    detail: str
    fatal: bool = True  # a failing fatal check stops the run before anything happens


def validate_configuration(settings: PipelineSettings, initialize_db: bool = True) -> list[ConfigCheck]:
    """Everything the pipeline needs, checked up front. Never creates the browser profile.

    initialize_db=False (--check-config): read-only. The database is inspected with
    db.inspect_database_readonly() instead of init_db(), so no file, table or migration is created.
    Nothing here opens a browser or contacts a service."""
    checks = []
    mode_ok = settings.mode in MODES
    checks.append(ConfigCheck("APPLICATION_MODE", mode_ok,
                              settings.mode if mode_ok else f"{settings.mode!r} is not SAFE or AUTO"))
    if initialize_db:
        try:
            init_db()
            checks.append(ConfigCheck("SQLite database", True, str(DB_PATH)))
            confirmed = count_successful_applications()
        except Exception as e:  # noqa: BLE001 -- reported, the run stops
            checks.append(ConfigCheck("SQLite database", False, f"{type(e).__name__}: {e}"))
            confirmed = 0
    else:
        database = inspect_database_readonly()
        checks.append(readonly_database_check(database))
        confirmed = database["confirmed_applications"] or 0
    if settings.mode == AUTO:
        checks.append(ConfigCheck(
            "AUTO mode readiness", confirmed > 0,
            f"{confirmed} confirmed application(s) in history" if confirmed else
            "AUTO is refused until one SAFE-mode application has been confirmed end to end "
            "(set APPLICATION_MODE=SAFE and apply to one job first)"))
    checks.append(ConfigCheck("APPLICATION_JOB_WINDOW_HOURS", settings.window_hours > 0, f"{settings.window_hours}h"))
    checks.append(export_limit_check(AI_AGENT_EXPORT_LIMIT_SETTING, settings.export_limit))
    threshold_ok = 1 <= float(settings.score_threshold) <= 10
    checks.append(ConfigCheck("APPLICATION_SCORE_THRESHOLD", threshold_ok,
                              f"{settings.score_threshold:g}/10" if threshold_ok else "must be between 1 and 10"))
    verified = verified_experience(settings)
    checks.append(ConfigCheck(
        "Verified resume experience", verified is not None,
        f"{verified.text} (from {verified.source})" if verified else
        f"no '<N>+ years of experience' in the master resume ({settings.master_resume_path}): "
        "every job goes to REVIEW_REQUIRED and nothing is applied", fatal=False))
    profile = Path(settings.chrome_profile)
    checks.append(ConfigCheck(
        "Naukri browser profile", profile.is_dir() and any(profile.iterdir()),
        str(profile) if profile.is_dir() else
        f"{profile} not found: log in once with  python main.py --action naukri-login"))
    agent_dir = Path(settings.agent_dir) if settings.agent_dir else None
    checks.append(ConfigCheck(
        "AI Agent folder", bool(agent_dir and (agent_dir / "scripts").is_dir()),
        str(agent_dir) if agent_dir and (agent_dir / "scripts").is_dir() else
        "AI_AGENT_DIR / AI_AGENT_OUTPUT_DIR not set or not the AI Agent checkout: scoring is skipped", fatal=False))
    scoped = bool(agent_dir and (agent_dir / "scripts" / "job_scope.py").exists())
    checks.append(ConfigCheck(
        "AI Agent scoped runs", scoped,
        "supported: only the jobs exported each run are scored/tailored" if scoped else
        "this Agent version has no scripts/job_scope.py: it will not be run (no unscoped fallback)", fatal=False))
    python = resolve_agent_python(agent_dir, settings.agent_python)
    checks.append(ConfigCheck("AI Agent Python", python is not None,
                              str(python) if python else "not found: set AI_AGENT_PYTHON", fatal=False))
    checks.append(ConfigCheck("AI_AGENT_OUTPUT_DIR", settings.agent_output_dir is not None,
                              str(settings.agent_output_dir) if settings.agent_output_dir else
                              "not set: AI Agent scores cannot be imported", fatal=False))
    if settings.sheets_enabled:
        sheet_id = settings.resolved_sheet_id()
        checks.append(ConfigCheck("Google Sheet", sheet_id is not None,
                                  "configured" if sheet_id else
                                  "no GOOGLE_SHEET_ID and no google_sheet_id in the AI Agent's .env: results stay pending",
                                  fatal=False))
        command = resolve_gws_command()
        found = command[0] == "node" or shutil.which(command[0]) is not None
        checks.append(ConfigCheck("gws CLI", found, " ".join(Path(c).name for c in command) if found else
                                  "gws not found on PATH: results stay pending", fatal=False))
    else:
        checks.append(ConfigCheck("Google Sheets", True, "disabled (GOOGLE_SHEETS_ENABLED=false)", fatal=False))
    return checks


def readonly_database_check(database: dict) -> ConfigCheck:
    """The SQLite check for --check-config, from db.inspect_database_readonly() (nothing is written).
    A database that does not exist yet or misses tables is a warning: the next pipeline run's
    init_db() creates them. An unreadable database is fatal, as it is for a run."""
    path = database["path"]
    if database["error"]:
        return ConfigCheck("SQLite database", False, f"{path}: {database['error']}")
    if not database["exists"]:
        detail = ("not created yet: the first pipeline run copies the legacy jobs.db here"
                  if database["legacy_migration_pending"] else "not created yet: the first pipeline run creates it")
        return ConfigCheck("SQLite database", False, f"{path} {detail} (read-only check)", fatal=False)
    if database["missing_tables"]:
        return ConfigCheck("SQLite database", False,
                           f"{path} has no {', '.join(database['missing_tables'])} table(s) yet: the next "
                           "pipeline run creates them (read-only check)", fatal=False)
    confirmed = database["confirmed_applications"]
    return ConfigCheck("SQLite database", True,
                       f"{path} (read-only check, {confirmed} confirmed application(s) in history)")


def export_limit_check(raw: str, effective: int) -> ConfigCheck:
    """AI_AGENT_EXPORT_LIMIT as configured vs as used. Never fatal: an invalid value falls back (see config)."""
    detail = (f"{effective} job(s)/run; deferred fresh jobs are offered again next run, a low cap can let "
              "them expire before scoring, a higher cap means more AI calls")
    if not raw:
        return ConfigCheck("AI_AGENT_EXPORT_LIMIT", True, f"{detail} (unset: default 2)", fatal=False)
    try:
        value = int(raw)
    except ValueError:
        return ConfigCheck("AI_AGENT_EXPORT_LIMIT", False, f"{raw!r} is not a whole number: using {effective}",
                           fatal=False)
    if value < 1:
        return ConfigCheck("AI_AGENT_EXPORT_LIMIT", False, f"{raw!r} is below 1: using {effective}", fatal=False)
    return ConfigCheck("AI_AGENT_EXPORT_LIMIT", True, detail, fatal=False)


def configuration_ok(checks: list[ConfigCheck]) -> bool:
    return all(c.ok for c in checks if c.fatal)


def print_configuration(checks: list[ConfigCheck], out: Callable[[str], None] = print) -> None:
    out("CONFIGURATION")
    for check in checks:
        mark = "OK  " if check.ok else ("FAIL" if check.fatal else "WARN")
        out(f"  [{mark}] {check.name:<30} {check.detail}")


# ---------------------------------------------------------------------------
# Eligibility (same rules as `main.py --action application-candidates`)
# ---------------------------------------------------------------------------


def verified_experience(settings: PipelineSettings) -> Optional[VerifiedExperience]:
    """The experience the master ATS resume explicitly supports (never inflated)."""
    return verified_resume_experience(settings.master_resume_path)


def evaluate_candidate(job: dict, settings: PipelineSettings, reference_time: datetime,
                       agent_output_dir: Optional[Path] = None,
                       verified: Optional[VerifiedExperience] = None) -> Eligibility:
    """The Phase 3.4 rules for one job, with its verified resume, application history and the strict
    experience check against the master resume.

    When the resume is what blocks the job, the reason carries the resolver's own code and reason (missing
    resume, record for another job, unverified version, missing file ...) instead of only "not ready", and
    says so when this run's Agent stages never tailor. Only the reason text changes, never `eligible`."""
    artifact, resume_code, resume_reason = resolve_verified_resume(
        job["url"], agent_output_dir or settings.agent_output_dir)
    result = evaluate_job(
        dict(job, history_applied=has_successful_application(job)),
        reference_time=reference_time,
        preferred_locations=settings.preferred_locations,
        applicant_experience=verified if verified is not None else verified_experience(settings),
        score_threshold=settings.score_threshold,
        resume_ready=artifact is not None and artifact.verified,
        window_hours=settings.window_hours,
    )
    if result.reason == RESUME_NOT_READY_REASON and resume_code:
        detail = f"{RESUME_NOT_READY_REASON} ({resume_code}: {resume_reason})"
        if "tailor_job.py" not in settings.agent_stages:
            detail += "; this workflow's AI Agent stages do not include tailor_job.py, so no resume is created"
        result = replace(result, reason=detail)
    return result


def already_applied(job: dict) -> bool:
    """This exact job is applied: Naukri said so, or SQLite history has a confirmed application."""
    return bool(job.get("is_already_applied")) or job.get("application_state") in ("applied", "already_applied") \
        or has_successful_application(job)


def mode_prompt(mode: str, prompt: Callable[[str], str] = input,
                out: Callable[[str], None] = print) -> Callable[[str], str]:
    """The answer function handed to ApplicationRunner.

    SAFE: the human answers everything (exact y/yes approval, as before). AUTO: only the review
    approval is answered 'y'; every other question is declined ('s' stop / 'q' quit), so anything
    outside the configured facts goes to manual action or recovery -- never guessed."""
    if mode != AUTO:
        return prompt

    def auto(message: str) -> str:
        if message.startswith(APPROVAL_PROMPT):
            out(f"{message}y   [AUTO mode: approved after all eligibility checks passed]")
            return "y"
        answer = "s" if "s = stop" in message or "s to leave" in message else "q"
        out(f"{message}{answer}   [AUTO mode: needs a human; not answered]")
        return answer

    return auto


# ApplicationRunner prompts, as the auto-apply workflow answers them.
SUBMIT_ANSWERS_PROMPT = "Submit these answers?"  # asked only when every field was planned from configured facts
VERIFICATION_PROMPT = "Press Enter when done, or q to stop"  # CAPTCHA / verification page before Apply


def auto_apply_prompt(prompt: Callable[[str], str] = input,
                      out: Callable[[str], None] = print) -> Callable[[str], str]:
    """The answer function for the auto-apply workflow (APPLICATION_MODE=AUTO).

    - review approval: 'y' -- the job passed eligibility, the current-run scope, identity and resume checks;
    - 'Submit these answers?': 'y' -- ApplicationRunner asks it only after every form field was planned
      from configured APPLICANT_* facts; any field it cannot fill from them is handed off before this;
    - CAPTCHA / verification page before Apply: a real pause for YOU (the runner re-checks the page
      after Enter; q, or no console, stops the run). Nothing is solved or bypassed;
    - any other manual step (unanswerable question, manual handoff): 's' -- that job is left for manual
      completion and recorded; nothing is guessed."""
    def answer(message: str) -> str:
        if message.startswith(APPROVAL_PROMPT):
            out(f"{message}y   [auto-apply: approved after every eligibility, scope, identity and resume check]")
            return "y"
        if message.startswith(SUBMIT_ANSWERS_PROMPT):
            out(f"{message}y   [auto-apply: every answer came from your configured facts]")
            return "y"
        if message.startswith(VERIFICATION_PROMPT):
            out("[auto-apply: Naukri shows a verification page; complete it yourself in the browser]")
            return prompt(message)
        reply = "s" if "s = stop" in message or "s to leave" in message else "q"
        out(f"{message}{reply}   [auto-apply: needs a human; left for manual completion]")
        return reply

    return answer


# Per-application outcomes of auto-apply runs (result.json "applications").
APPLIED_CONFIRMED = "APPLIED_CONFIRMED"
APPLICATION_FAILED = "APPLICATION_FAILED"
APPLICATION_SKIPPED = "APPLICATION_SKIPPED"
AUTH_REQUIRED_OUTCOME = "AUTH_REQUIRED"
CAPTCHA_REQUIRED_OUTCOME = "CAPTCHA_REQUIRED"
APPLICATION_STATUS_UNKNOWN = "APPLICATION_STATUS_UNKNOWN"
# After these, the browser or the account is in a state another automatic application must not follow.
STOP_AFTER_OUTCOMES = (AUTH_REQUIRED_OUTCOME, CAPTCHA_REQUIRED_OUTCOME, APPLICATION_STATUS_UNKNOWN)


def submission_outcome(state: Optional[str], code: Optional[str]) -> str:
    """One label per job. Only Naukri's own confirmation ('applied') is APPLIED_CONFIRMED; an outcome
    Apply may have caused that nobody confirmed (recovery_required) is STATUS_UNKNOWN, never success."""
    if state == "applied":
        return APPLIED_CONFIRMED
    if state == "recovery_required":
        return APPLICATION_STATUS_UNKNOWN
    if code == "CAPTCHA_REQUIRED":
        return CAPTCHA_REQUIRED_OUTCOME
    if state == "requires_login" or code == "AUTH_REQUIRED":
        return AUTH_REQUIRED_OUTCOME
    if state in ("failed", "submission_failed"):
        return APPLICATION_FAILED
    return APPLICATION_SKIPPED  # already applied, external, unavailable, not ready, manual completion, ...


def _job_identity(job: dict) -> dict:
    return {"job_id": job.get("id"), "naukri_job_id": job.get("job_id"), "title": job.get("title"),
            "company": job.get("company")}


def _scope_key(job: dict) -> str:
    """The id this job carries in the scope file (ai_agent_bridge.scope_id of its export record)."""
    return str(job.get("job_id") or job.get("dedup_key") or agent_canonical_link(job.get("url")))


def validate_run_scope(scope_path: Optional[Path], run_id: Optional[str], expected: dict) -> dict:
    """Re-read this run's scope file and require it to be exactly what this run exported:
    the same run_id and the same set of job ids. expected: {jobs.id: scope id}. Read-only.
    Returns {"ok", "detail", "run_id", "scope_file", "job_ids"}; ok=False means nothing may be applied."""
    report = {"ok": False, "detail": "", "run_id": run_id, "scope_file": str(scope_path) if scope_path else None,
              "job_ids": sorted(expected)}
    if not run_id or scope_path is None or not expected:
        report["detail"] = "nothing was exported this run"
        return report
    try:
        data = json.loads(Path(scope_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        report["detail"] = f"scope file unreadable: {type(e).__name__}"
        return report
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        report["detail"] = 'scope file is not an object with a "jobs" list'
        return report
    if data.get("run_id") != run_id:
        report["detail"] = f"scope file belongs to run {data.get('run_id')!r}, not this run {run_id!r}"
        return report
    ids = [str(entry.get("id")) for entry in data["jobs"] if isinstance(entry, dict)]
    if len(ids) != len(data["jobs"]) or sorted(ids) != sorted(expected.values()):
        report["detail"] = "scope file jobs differ from the jobs this run exported"
        return report
    report.update(ok=True, detail="scope file matches this run's export")
    return report


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    mode: str = SAFE
    dry_run: bool = False
    window_hours: int = 24
    naukri_login: str = "SKIPPED"
    jobs_scanned: int = 0
    new_jobs: int = 0
    duplicates_removed: int = 0
    skipped_stale_cards: int = 0  # search cards already older than the window (detail page never opened)
    skipped_experience_cards: int = 0  # search cards whose Naukri experience minimum exceeds the resume
    description_missing: int = 0  # job pages without a readable description (not stored; retried next scan)
    scan_status: str = "SKIPPED"
    fresh_jobs: int = 0
    already_applied: int = 0
    # Export funnel over stored, unscored ('discovered') jobs.
    discovered_unscored: int = 0  # all stored jobs still waiting for an AI score
    not_fresh_unscored: int = 0  # of those, not fresh (STALE or UNKNOWN): never exported
    export_candidates: int = 0  # fresh, never applied, experience ELIGIBLE: offered to the export
    export_rejected: int = 0  # offered but not a valid Agent record (e.g. description < 80 chars)
    export_deferred: int = 0  # valid but past AI_AGENT_EXPORT_LIMIT: offered again next run if still fresh
    export_limit: int = 0
    exported_ids: list = field(default_factory=list)  # jobs.id of the jobs exported this run
    agent_export_id: Optional[str] = None  # run_id of this run's export/scope file; None = nothing exported
    # What the AI Agent newly scored / tailored during this run (from its output files, before vs after):
    # jobs this run exported vs anything else it processed (e.g. its own cached LinkedIn jobs).
    agent_new_scored: Optional[int] = None
    agent_new_scored_other: Optional[int] = None
    agent_new_tailored: Optional[int] = None
    agent_new_tailored_other: Optional[int] = None
    agent_new_in_store: Optional[int] = None  # exported jobs the Agent's raw_jobs.json did not hold before
    # The Agent's own per-run report of the scoped run (ids from the scope file); None = unknown.
    scope_report: Optional[dict] = None
    # Scores imported into SQLite for this run's exported jobs (others imported are historical Agent data).
    exported_scored: int = 0
    exported_recommended: int = 0
    # Experience gate results, by where they happened.
    export_experience_ineligible: int = 0  # unscored fresh jobs, before export (no AI call spent)
    export_experience_review: int = 0
    experience_ineligible: int = 0  # INELIGIBLE: skipped before scoring/tailoring or at eligibility
    experience_review: int = 0  # REVIEW_REQUIRED: requirement unreadable or conflicting; never applied
    exported: int = 0
    agent_status: str = "SKIPPED"
    scores_imported: int = 0
    recommended: int = 0
    review: int = 0
    skipped_scores: int = 0
    candidates: int = 0
    eligible: int = 0
    applied: int = 0
    application_skipped: int = 0
    failed: int = 0
    recovery_required: int = 0
    dry_run_ready: int = 0
    # auto-apply runs only (None = not an auto-apply run, or the step did not run)
    scope_validation: Optional[dict] = None  # this run's scope file re-checked before eligibility
    candidates_outside_run: Optional[int] = None  # recommended jobs from earlier runs: never evaluated here
    eligibility_results: Optional[list] = None  # one record per current-run candidate
    applications: Optional[list] = None  # one record per job the application step decided on
    stopped: Optional[str] = None
    sheets: SyncReport = field(default_factory=lambda: SyncReport(status="SKIPPED"))
    warnings: list = field(default_factory=list)
    fatal: Optional[str] = None

    @property
    def exit_code(self) -> int:
        if self.fatal:
            return EXIT_CONFIG
        if self.naukri_login == "AUTH_REQUIRED" or self.failed or self.recovery_required or self.warnings:
            return EXIT_ATTENTION
        return EXIT_OK


class ApplicationPipeline:
    def __init__(
        self,
        settings: Optional[PipelineSettings] = None,
        *,
        dry_run: bool = False,
        skip_scan: bool = False,
        skip_agent: bool = False,
        no_apply: bool = False,
        sync_only: bool = False,
        login_wait_minutes: int = 0,
        auto_apply: bool = False,
        scanner_factory: Optional[Callable] = None,
        browser_factory: Optional[Callable] = None,
        runner_factory: Optional[Callable] = None,
        agent_runner: Callable[..., AgentRunResult] = run_agent_stages,
        tracker_factory: Optional[Callable] = None,
        prompt: Callable[[str], str] = input,
        out: Callable[[str], None] = print,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.settings = settings or PipelineSettings()
        self.dry_run, self.skip_scan, self.skip_agent = dry_run, skip_scan, skip_agent
        self.no_apply, self.sync_only = no_apply, sync_only
        # > 0 (--wait-for-login): when the session check finds the login page, open Naukri's login page in
        # the same visible browser and wait this long for a manual login (never typed for you), once.
        self.login_wait_minutes = login_wait_minutes
        # auto-apply workflow: candidates only from this run's validated scope, auto_apply_prompt, the Naukri
        # job identity checked before Apply, per-job records. Never set by the CLI default.
        self.auto_apply = auto_apply
        self._run_scope: tuple = (None, None, {})  # (scope file, run_id, {jobs.id: scope id}) of this export
        self.scanner_factory = scanner_factory or _default_scanner
        self.browser_factory = browser_factory or _default_browser
        self.runner_factory = runner_factory or _default_runner
        self.agent_runner = agent_runner
        self.tracker_factory = tracker_factory or _default_tracker
        self.prompt, self.out, self.clock = prompt, out, clock
        self.summary = RunSummary(mode=self.settings.mode, dry_run=dry_run, window_hours=self.settings.window_hours)
        self._verified: Optional[VerifiedExperience] = None
        self._verified_loaded = False

    @property
    def verified(self) -> Optional[VerifiedExperience]:
        if not self._verified_loaded:
            self._verified, self._verified_loaded = verified_experience(self.settings), True
        return self._verified

    def _experience_rejected(self, job: dict, status: str, reason: str, before_export: bool = False) -> None:
        s = self.summary
        if status == INELIGIBLE:
            s.experience_ineligible += 1
            s.export_experience_ineligible += int(before_export)
        elif status == REVIEW_REQUIRED:
            s.experience_review += 1
            s.export_experience_review += int(before_export)
        self.out(f"  [{job['id']}] {job.get('title') or '?'} @ {job.get('company') or '?'}: {reason}")

    # -- steps -------------------------------------------------------------------------

    def run(self) -> RunSummary:
        s = self.summary
        checks = validate_configuration(self.settings)  # 1
        print_configuration(checks, self.out)
        if not configuration_ok(checks):
            s.fatal = "configuration invalid: " + "; ".join(f"{c.name}: {c.detail}" for c in checks
                                                            if c.fatal and not c.ok)
            return s
        if self.sync_only:
            self.sync_sheets()
            return s
        will_browse = (not self.skip_scan and self.settings.naukri_enabled) or not self.no_apply
        if will_browse:
            self.check_session()  # 2
        if not self.skip_scan:
            self.scan()  # 3
        fresh_pending = self.filter_fresh()  # 4, 5
        self.export_and_score(fresh_pending)  # 6, 7, 8
        eligible = self.eligibility()  # 9, 10
        if self.no_apply:
            self.out("Applications: skipped (--no-apply)")
        elif s.naukri_login != "OK":
            self.out(f"Applications: skipped (Naukri login: {s.naukri_login}). "
                     "Log in once with  python main.py --action naukri-login")
        else:
            self.apply(eligible)  # 11-14
        if self.dry_run:
            s.sheets = SyncReport(status="SKIPPED (dry run)", pending=0)
        else:
            self.sync_sheets()  # 15, 16
        return s

    def check_session(self) -> None:
        try:
            with self.browser_factory() as browser:
                state = browser.session_state()
                if state == "login_required" and self.login_wait_minutes > 0:
                    self.out(f"Naukri login required. Log in by hand in the opened browser window (OTP/CAPTCHA "
                             f"included); waiting up to {self.login_wait_minutes} minute(s). Nothing is typed for you.")
                    state = "authenticated" if browser.wait_for_login(self.login_wait_minutes, out=self.out) \
                        else "login_required"
                    self.out("Naukri login confirmed; continuing." if state == "authenticated" else
                             "Naukri login not completed in time; continuing without it (the scan may stop at a "
                             "login page). Not retried.")
        except Exception as e:  # noqa: BLE001 -- profile locked, browser missing, network
            code = getattr(e, "code", None) or type(e).__name__
            self.summary.naukri_login = "ERROR"
            self.summary.warnings.append(f"Naukri session check failed: {code}: {e}")
            return
        self.summary.naukri_login = {"authenticated": "OK", "login_required": "AUTH_REQUIRED"}.get(state, "AUTH_REQUIRED")
        if state == "unknown":
            self.summary.warnings.append("Naukri login could not be confirmed (no logged-in header found)")

    def scan(self) -> None:
        s = self.summary
        if not self.settings.naukri_enabled:
            s.scan_status = "DISABLED (NAUKRI_ENABLED=false)"
            return
        scanner = self.scanner_factory()
        try:
            result = scanner.scan()
        except Exception as e:  # noqa: BLE001 -- stored jobs are still processed below
            s.scan_status = "FAILED"
            s.warnings.append(f"Naukri scan failed: {type(e).__name__}: {e}")
            return
        finally:
            scanner.close()
        s.jobs_scanned, s.new_jobs, s.duplicates_removed = result.unique_jobs, result.new_jobs_stored, result.duplicates_skipped
        s.skipped_stale_cards = getattr(result, "skipped_stale", 0)
        s.skipped_experience_cards = getattr(result, "skipped_experience", 0)
        s.description_missing = getattr(result, "description_missing", 0)
        s.scan_status = f"STOPPED ({result.stopped_reason})" if result.stopped_reason else "OK"
        if result.stopped_reason:
            s.warnings.append(f"Naukri scan stopped early: {result.stopped_reason}")

    def filter_fresh(self) -> list[dict]:
        """Fresh (within the window) stored jobs that still need anything; already-applied ones are
        counted and dropped. Returns the fresh, never-applied, unscored jobs to export.

        The strict experience check runs here, before export, so a job whose JD needs more experience
        than the master resume supports (or whose requirement is unreadable) never reaches AI scoring
        or tailoring. The result is stored on the jobs row."""
        reference = self.clock()
        pending = []
        header = False
        for job in get_jobs(status=None):
            unscored = job.get("status") == JOB_STATUS_DISCOVERED
            self.summary.discovered_unscored += int(unscored)
            if job.get("application_state") in FINAL_APPLICATION_STATES and not already_applied(job):
                if unscored:
                    logger.debug(f"Not exported | db_id={job['id']} | application state {job.get('application_state')}")
                continue  # external / unavailable: nothing left to do
            freshness = job_freshness(job, reference_time=reference, window_hours=self.settings.window_hours)[0]
            if freshness != FRESH:
                if unscored:
                    self.summary.not_fresh_unscored += 1
                    logger.debug(f"Not exported | db_id={job['id']} | freshness {freshness} "
                                 f"(posted {job.get('posted_label') or '?'})")
                continue
            self.summary.fresh_jobs += 1
            if already_applied(job):
                self.summary.already_applied += 1
            elif job.get("status") == JOB_STATUS_DISCOVERED:
                check = record_experience_check(job, self.verified)
                if check.status == ELIGIBLE:
                    pending.append(job)
                    continue
                if not header:
                    self.out("Experience check (before AI scoring / tailoring):")
                    header = True
                self._experience_rejected(job, check.status, check.reason, before_export=True)
        return pending

    def export_and_score(self, fresh_pending: list[dict]) -> None:
        s = self.summary
        wanted = {job["id"] for job in fresh_pending}
        s.export_candidates, s.export_limit = len(fresh_pending), self.settings.export_limit
        if self.dry_run:
            s.exported = min(len(fresh_pending), self.settings.export_limit)
            s.export_deferred = max(0, len(fresh_pending) - s.exported)
            s.agent_status = "SKIPPED (dry run)"
        else:
            # Relevance first (target Java / Full Stack roles, matching skills), then expiry risk, then id:
            # with a cap of 2, the order decides which fresh jobs get scored at all (export_priority.py).
            self._export_reference = self.clock()
            export = export_discovered_jobs(self.settings.export_path, self.settings.export_limit,
                                            job_filter=lambda job: job["id"] in wanted,
                                            order_key=relevance_order(self._export_reference,
                                                                      self.settings.window_hours),
                                            order_label=EXPORT_ORDER_LABEL)
            s.exported, s.export_rejected, s.export_deferred = (len(export.exported), len(export.skipped),
                                                                len(export.deferred))
            s.exported_ids = export.exported_ids
            if export.exported:
                s.agent_export_id = export.run_id
                # EXPORTED decisions and export.exported are appended together, in the same order.
                self._run_scope = (export.scope_path, export.run_id,
                                   {job_id: bridge_scope_id(record)
                                    for job_id, record in zip(export.exported_ids, export.exported)})
            self._print_export_decisions(fresh_pending, export.decisions)
            if self.skip_agent:
                s.agent_status = "SKIPPED (--skip-agent)"
            elif not export.exported:
                s.agent_status = "SKIPPED (no fresh unscored jobs)"
            else:
                before = agent_output_links(self.settings.agent_output_dir)
                # Scoped: only the jobs exported above may be scored or tailored. A failure is reported
                # as it is; there is no retry without the scope.
                result = self.agent_runner(self.settings.agent_dir, self.settings.agent_stages,
                                           self.settings.export_path, self.settings.agent_python,
                                           self.settings.agent_timeout_minutes, scope_path=export.scope_path)
                s.agent_status = "OK" if result.ok else f"{result.code}: {result.message}"
                if not result.ok:
                    s.warnings.append(f"AI Agent: {result.code}: {result.message}")
                self._account_agent_activity(before, agent_output_links(self.settings.agent_output_dir),
                                             {agent_canonical_link(r["link"]) for r in export.exported})
                s.scope_report = read_scope_report(self.settings.agent_output_dir, export.run_id)
                if result.ok and s.scope_report is None:
                    s.warnings.append("AI Agent finished but wrote no report for this scoped run "
                                      f"(run {export.run_id}): newly scored/tailored counts are unknown")
        if self.dry_run or self.settings.agent_output_dir is None:
            return
        scored_path = Path(self.settings.agent_output_dir) / "scored_jobs.json"
        if not scored_path.exists():
            return
        try:
            imported = import_agent_scores(scored_path)
        except (ValueError, OSError) as e:
            s.warnings.append(f"Score import failed: {e}")
            return
        s.scores_imported = imported.scored_now
        s.recommended = imported.by_status.get("recommended", 0)
        s.review = imported.by_status.get("review", 0)
        s.skipped_scores = imported.by_status.get("skipped", 0)
        for job_id in s.exported_ids:  # this run's own results, apart from historical Agent data
            job = get_job(job_id)
            if job and job.get("scored_at"):
                s.exported_scored += 1
                s.exported_recommended += int(job.get("match_status") == "recommended")

    def _print_export_decisions(self, fresh_pending: list[dict], decisions: list) -> None:
        """One line per fresh, experience-eligible, unscored job: exported, deferred or rejected, and why."""
        if not decisions:
            return
        jobs = {job["id"]: job for job in fresh_pending}
        counts = Counter(decision for _, decision, _ in decisions)
        self.out(f"Export to AI Agent: {len(decisions)} fresh, experience-eligible, unscored job(s) -> "
                 + ", ".join(f"{counts[d]} {d.lower()}" for d in ("EXPORTED", "DEFERRED", "REJECTED") if counts[d]))
        reference = getattr(self, "_export_reference", None)
        for job_id, decision, reason in decisions:
            job = jobs.get(job_id, {})
            rank = (f" [{export_priority(job, reference, self.settings.window_hours).describe()}]"
                    if job and reference is not None else "")
            self.out(f"  [{job_id}] {decision:<8} {job.get('title') or '?'} @ {job.get('company') or '?'}{rank}"
                     + (f": {reason}" if reason else ""))

    def _account_agent_activity(self, before: Optional[tuple], after: Optional[tuple], exported: set) -> None:
        """Separate what the Agent newly scored/tailored for this run's export from anything else it
        processed (its outputs are cumulative and also hold its own cached jobs)."""
        if before is None or after is None:
            return  # output files unreadable: unknown, not zero
        s = self.summary
        new_scored, new_tailored = after[0] - before[0], after[1] - before[1]
        s.agent_new_in_store = len(exported - before[2])
        s.agent_new_scored, s.agent_new_scored_other = len(new_scored & exported), len(new_scored - exported)
        s.agent_new_tailored, s.agent_new_tailored_other = len(new_tailored & exported), len(new_tailored - exported)
        if s.agent_new_scored_other or s.agent_new_tailored_other:
            s.warnings.append(
                f"AI Agent also scored {s.agent_new_scored_other} and tailored {s.agent_new_tailored_other} job(s) "
                "this run did not export (e.g. its own cached LinkedIn jobs); those did not pass this project's "
                "freshness or experience checks")

    def eligibility(self) -> list[dict]:
        reference = self.clock()
        eligible = []
        allowed = self._current_run_candidates() if self.auto_apply else None
        for job in get_application_candidates():
            if allowed is not None and allowed.get(job["id"]) != _scope_key(job):
                # an earlier run's recommendation (or a scope mismatch): never evaluated or applied here
                self.summary.candidates_outside_run += 1
                continue
            self.summary.candidates += 1
            result = evaluate_candidate(job, self.settings, reference, verified=self.verified)
            save_experience_check(job["id"], result.experience_check)
            if allowed is not None:
                self.summary.eligibility_results.append(dict(
                    _job_identity(job), eligible=result.eligible, reason=result.reason, score=result.score,
                    freshness=result.freshness_status, experience=result.experience_status,
                    location_match=result.location_match, resume_ready=result.resume_ready))
            if result.eligible:
                eligible.append((job, result))
            elif result.experience_status != ELIGIBLE:
                self._experience_rejected(job, result.experience_status, result.reason)
            else:
                self.out(f"  [{job['id']}] {job['title']} @ {job['company']}: not eligible: {result.reason}")
        eligible.sort(key=lambda item: (-priority_score(item[0], item[1]), item[0]["id"]))
        self.summary.eligible = len(eligible)
        return [job for job, _ in eligible]

    def _current_run_candidates(self) -> dict:
        """auto-apply: {jobs.id: scope id} of this run's export, after re-validating the scope file.
        An invalid or missing scope means no candidate at all -- never a fallback to history."""
        s = self.summary
        scope_path, run_id, expected = self._run_scope
        s.scope_validation = validate_run_scope(scope_path, run_id, expected)
        s.candidates_outside_run, s.eligibility_results = 0, []
        if not s.scope_validation["ok"]:
            if expected:  # something was exported but the scope cannot be trusted: say so
                s.warnings.append(f"Current-run scope rejected: {s.scope_validation['detail']}; nothing is applied")
            return {}
        return dict(expected)

    def apply(self, eligible: list[dict]) -> None:
        s = self.summary
        limit = self.settings.max_applications
        batch = eligible[:limit]
        if self.auto_apply:
            s.applications = [self._application_record(job, None, APPLICATION_SKIPPED, "PER_RUN_LIMIT",
                                                       f"over the per-run application limit (APPLICATION_MAX_JOBS={limit})")
                              for job in eligible[limit:]]
        if not batch:
            return
        self.out(f"\n{'DRY RUN - ' if self.dry_run else ''}{s.mode} mode: {len(batch)} application(s) this run"
                 + ("" if s.mode == AUTO else "; each one needs your exact 'y' or 'yes' before Apply is clicked"))
        answer = auto_apply_prompt(self.prompt, self.out) if self.auto_apply else mode_prompt(s.mode, self.prompt, self.out)
        attempted = set()
        runner = None
        try:
            with self.browser_factory() as browser:
                runner = self.runner_factory(browser, answer, self.out, self.dry_run)
                if self.auto_apply:
                    runner.require_job_identity = True  # the opened page must be this exact Naukri job
                for job in batch:
                    attempted.add(job["id"])
                    try:
                        result = runner.process(job)
                    except Exception as e:  # ApplicationStop (login, CAPTCHA, quit) or a browser failure
                        s.stopped = f"{getattr(e, 'code', type(e).__name__)}: {e}"
                        self._track(job)  # the runner already persisted whatever state it reached
                        if self.auto_apply:
                            s.applications.append(self._stopped_record(job, e, runner))
                        break
                    self._count(result)
                    self._track(job)
                    if self.auto_apply:
                        outcome = submission_outcome(result.state, result.code)
                        s.applications.append(self._application_record(
                            job, result.state, outcome, result.code, result.reason, runner, result))
                        if outcome in STOP_AFTER_OUTCOMES:  # browser/account state not safe to continue
                            s.stopped = f"{outcome} on job {job['id']}: no further automatic application this run"
                            break
        except Exception as e:  # noqa: BLE001 -- the browser could not start
            s.stopped = f"{getattr(e, 'code', type(e).__name__)}: {e}"
        if self.auto_apply:
            for job in batch:
                if job["id"] not in attempted or not any(r["job_id"] == job["id"] for r in s.applications):
                    s.applications.append(self._application_record(
                        job, None, APPLICATION_SKIPPED, "NOT_ATTEMPTED", f"not attempted: applications stopped "
                                                                         f"({s.stopped})", runner))
        if s.stopped:
            s.warnings.append(f"Applications stopped: {s.stopped}")

    def _application_record(self, job: dict, state: Optional[str], outcome: str, code: Optional[str],
                            reason: str, runner=None, result=None) -> dict:
        """One auto-apply result.json entry. Unknown parts are None, never guessed."""
        resume = getattr(result, "resume", None) if result is not None else None
        identity = getattr(runner, "identity_checks", {}).get(job["id"]) if runner is not None else None
        return dict(_job_identity(job), outcome=outcome, state=state, code=code, reason=reason,
                    confirmation_signal=getattr(result, "confirmation_signal", None),
                    identity_check=identity,
                    resume={"version": getattr(resume, "version", None),
                            "file_type": resume.path.suffix[1:] if getattr(resume, "path", None) else None,
                            "uploadable": getattr(resume, "uploadable", None)} if resume is not None else None,
                    manual_completion_required=state == "requires_manual_action")

    def _stopped_record(self, job: dict, error: Exception, runner) -> dict:
        """The job a run stopped on (login, CAPTCHA, quit, browser failure): as the runner persisted it."""
        stored = get_job(job["id"]) or job
        state = stored.get("application_state")
        code = getattr(error, "code", None) or stored.get("application_code")
        outcome = submission_outcome(state, code)
        if outcome == APPLICATION_SKIPPED and state not in (None, "ready", "not_ready", "preparing"):
            outcome = APPLICATION_FAILED  # stopped part-way, nothing confirmed
        return self._application_record(job, state, outcome, code, str(error), runner)

    def _count(self, result) -> None:
        s = self.summary
        if self.dry_run:
            s.dry_run_ready += int(result.state == "ready")
            return
        label = application_outcome_label(result.state, result.code)
        if result.state == "applied":
            s.applied += 1
        elif label == OUTCOME_RECOVERY_REQUIRED:
            s.recovery_required += 1
        elif label == OUTCOME_FAILED:
            s.failed += 1
        else:  # not approved, already applied on Naukri, external, unavailable
            s.application_skipped += 1

    def _track(self, job: dict) -> None:
        if not self.dry_run:
            queue_sheet_sync(job["id"])  # SQLite already holds the outcome; this only queues reporting

    def sync_sheets(self) -> None:
        settings = self.settings
        if not settings.sheets_enabled:
            self.summary.sheets = sync_pending_outcomes(None, DISABLED)
            return
        sheet_id = settings.resolved_sheet_id()
        if not sheet_id:
            self.summary.sheets = sync_pending_outcomes(None, SHEET_NOT_CONFIGURED, "no Google Sheet id configured")
            return
        self.summary.sheets = sync_pending_outcomes(self.tracker_factory(sheet_id, settings.sheet_tab))


def run_eligibility_check(settings: PipelineSettings, out: Callable[[str], None] = print,
                          clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    """Non-submitting report: the experience check for every stored, not-yet-applied job (stored on its
    row) and the full eligibility of the application candidates. No browser, no export, no AI Agent,
    no Google Sheets, nothing is applied. Returns {status: [job, ...]} for the candidates."""
    init_db()
    verified = verified_experience(settings)
    reference = clock()
    out(f"Verified resume experience: {verified.text + ' (' + verified.source + ')' if verified else 'NOT FOUND'}")
    out(f"Mode: {settings.mode} (this check never applies)\n")
    totals = {ELIGIBLE: 0, INELIGIBLE: 0, REVIEW_REQUIRED: 0}
    review = []
    for job in get_jobs(status=None):
        if already_applied(job) or job.get("application_state") in FINAL_APPLICATION_STATES:
            continue
        check = record_experience_check(job, verified)
        totals[check.status] += 1
        if check.status == REVIEW_REQUIRED:
            review.append((job, check))
    out("EXPERIENCE CHECK - all stored jobs not yet applied")
    for status, count in totals.items():
        out(f"  {status:<16}{count:>5}")
    for job, check in review:
        out(f"  [{job['id']}] {job.get('title')} @ {job.get('company')}: {check.reason}")

    groups = {"ELIGIBLE": [], "SKIPPED": [], "REVIEW_REQUIRED": [], "NOT ELIGIBLE (other rule)": []}
    for job in get_application_candidates():
        result = evaluate_candidate(job, settings, reference, verified=verified)
        if result.eligible:
            key = "ELIGIBLE"
        elif result.experience_status == INELIGIBLE:
            key = "SKIPPED"
        elif result.experience_status == REVIEW_REQUIRED:
            key = "REVIEW_REQUIRED"
        else:
            key = "NOT ELIGIBLE (other rule)"
        groups[key].append((job, result))
    out("\nAPPLICATION CANDIDATES (scored 'recommended', not applied)")
    for key, items in groups.items():
        out(f"  {key}: {len(items)}")
        for job, result in items:
            out(f"    [{job['id']}] {job['title']} @ {job['company']} | JD {result.experience_check.required_text or '-'}"
                f" | {result.freshness_status} | {result.reason}")
    return groups


def agent_output_links(output_dir: Optional[Path]) -> Optional[tuple[set, set]]:
    """(canonical links in the Agent's scored_jobs.json, links saved in its tailored_jobs.json, links in
    its raw_jobs.json store), read-only; None when they cannot be read. Missing files are empty sets."""
    if output_dir is None:
        return None
    sets = []
    for name, keep in (("scored_jobs.json", lambda e: True),
                       ("tailored_jobs.json", lambda e: e.get("status") == "saved"),
                       ("raw_jobs.json", lambda e: True)):
        path = Path(output_dir) / name
        try:
            entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError, ValueError):
            return None
        sets.append({agent_canonical_link(e["link"]) for e in entries
                     if isinstance(e, dict) and e.get("link") and keep(e)})
    return sets[0], sets[1], sets[2]


def _default_scanner():
    from naukri_scanner import NaukriScanner
    return NaukriScanner(headless=False)


def _default_browser():
    from naukri_application import NaukriApplicationBrowser
    return NaukriApplicationBrowser()


def _default_runner(browser, prompt, out, dry_run):
    from naukri_application import ApplicationRunner
    return ApplicationRunner(browser, prompt=prompt, out=out, dry_run=dry_run)


def _default_tracker(sheet_id: str, tab: str) -> SheetTracker:
    return SheetTracker(GwsClient(), sheet_id, tab)


# ---------------------------------------------------------------------------
# Summary and CLI
# ---------------------------------------------------------------------------


def _count(values) -> int:
    return len(values or [])


def _agent_lines(s: RunSummary, row) -> list[str]:
    """This run's scoped Agent results, from the Agent's own report for this run (ids from the scope
    file). Unknown -- never 0 -- when the Agent did not run, wrote no report for this run, or a stage
    did not record its part (e.g. tailoring stopped before writing it)."""
    if s.scope_report is None:
        unknown = "unknown (no report for this run)" if s.agent_status == "OK" else "-"
        return [row("Newly scored", unknown), row("Newly tailored", unknown)]
    score, tailor = s.scope_report.get("score"), s.scope_report.get("tailor")

    def count(section, key):
        return _count(section.get(key)) if isinstance(section, dict) else "unknown (stage did not report)"

    def pair(first, second):
        """'<n> scored, <m> tailored' style; each half is 'unknown' when its stage did not report."""
        parts = []
        for section, key, label in (first, second):
            value = count(section, key)
            parts.append(f"{value if isinstance(value, int) else 'unknown'} {label}")
        return ", ".join(parts)

    return [
        row("Newly scored", count(score, "newly_scored")),
        row("Newly tailored", count(tailor, "newly_tailored")),
        row("Already processed (skipped)", pair((score, "already_scored", "scored"), (tailor, "already_tailored", "tailored"))),
        row("Below the 8+ cutoff", count(tailor, "not_qualified")),
        row("Not in the Agent store", count(score, "missing")),
        row("Failed / flagged", pair((score, "failed", "scoring"), (tailor, "flagged", "tailoring"))),
    ]


def format_summary(s: RunSummary) -> str:
    wide, thin = "=" * 60, "-" * 60

    def row(label: str, value) -> str:
        return f"{label + ':':<38}{value:>6}" if isinstance(value, int) else f"{label + ':':<38}{value}"

    sheet_status = s.sheets.status
    candidate_ineligible = s.experience_ineligible - s.export_experience_ineligible
    candidate_review = s.experience_review - s.export_experience_review
    lines = [wide, "         NAUKRI AUTO JOB APPLICATION", wide, "",
             row("Mode", s.mode + (" (DRY RUN)" if s.dry_run else "")),
             row("Naukri login", s.naukri_login),
             row("Naukri scan", s.scan_status),
             row("Jobs scanned", s.jobs_scanned),
             row("New jobs stored", s.new_jobs),
             row("Duplicates removed", s.duplicates_removed),
             row(f"Skipped: older than {s.window_hours}h", s.skipped_stale_cards),
             row("Skipped: experience > resume", s.skipped_experience_cards),
             row("No description (retry)", s.description_missing),
             row(f"Fresh stored jobs (<{s.window_hours}h)", s.fresh_jobs),
             row("  already applied", s.already_applied),
             "", "EXPORT TO AI AGENT (this run)", thin,
             row("Unscored jobs evaluated", s.discovered_unscored),
             row(f"  not fresh (<{s.window_hours}h)", s.not_fresh_unscored),
             row("  experience: skipped", s.export_experience_ineligible),
             row("  experience: review required", s.export_experience_review),
             row("Fresh, experience-eligible", s.export_candidates),
             row("  rejected (invalid record)", s.export_rejected),
             row("  would export (dry run)" if s.dry_run else "  exported to AI Agent", s.exported),
             row(f"  deferred (limit {s.export_limit}/run)", s.export_deferred),
             "", "AI AGENT (this run's exported jobs only)", thin,
             row("AI Agent", s.agent_status),
             row("Agent export ID", s.agent_export_id or "-")]
    if s.exported and not s.dry_run:
        lines += _agent_lines(s, row)
        if s.agent_new_in_store is not None:
            lines.append(row("New to the Agent store", s.agent_new_in_store))
        if s.agent_new_scored_other is not None:
            lines.append(row("Other jobs the Agent processed", f"{s.agent_new_scored_other} scored, "
                                                                f"{s.agent_new_tailored_other} tailored"))
    lines += ["", "AI SCORING (imported into SQLite)", thin,
              row("This run's exports scored", s.exported_scored),
              row("  recommended (8+)", s.exported_recommended),
              row("All scores imported this run", s.scores_imported),
              row("  recommended / review / skipped", f"{s.recommended} / {s.review} / {s.skipped_scores}"),
              "", "ELIGIBILITY (application candidates)", thin,
              row("Recommended, not applied (all runs)", s.candidates),
              row("  experience: skipped", candidate_ineligible),
              row("  experience: review required", candidate_review),
              row("Eligible to apply now", s.eligible),
              "", "APPLICATIONS", thin]
    if s.dry_run:
        lines.append(row("Ready (dry run)", s.dry_run_ready))
    lines += [row("Applied", s.applied),
              row("Skipped", s.application_skipped),
              row("Failed", s.failed),
              row("Recovery required", s.recovery_required)]
    if s.scope_validation is not None:  # auto-apply run
        lines += ["", "AUTO-APPLY (this run's exported jobs only)", thin,
                  row("Current-run scope", "OK" if s.scope_validation["ok"] else
                      f"REJECTED ({s.scope_validation['detail']})"),
                  row("Earlier runs' jobs (not touched)", s.candidates_outside_run)]
        for record in s.applications or []:
            lines.append(f"  [{record['job_id']}] {record['outcome']:<27} {record['title'] or '?'} @ "
                         f"{record['company'] or '?'}" + (f": {record['reason']}" if record["reason"] else ""))
    lines += ["", "GOOGLE SHEETS", thin,
              row("Status", sheet_status),
              row("Rows written", s.sheets.synced),
              row("Pending sync", s.sheets.pending)]
    if s.fatal or s.warnings:
        lines += ["", "ATTENTION", thin] + [f"- {w}" for w in ([s.fatal] if s.fatal else []) + s.warnings]
    if s.recovery_required:
        lines.append("- Recover with: python main.py --action application-status")
    status = "RUN FAILED" if s.fatal else ("RUN COMPLETE (ATTENTION NEEDED)" if s.exit_code else "RUN COMPLETE")
    lines += ["", wide, status, wide]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run folders, run.log and result.json (--run-dir), and the read-only --last-run viewer
# ---------------------------------------------------------------------------

RUNS_DIR = BASE_DIR / "output" / "runs"  # start_local.bat's runs; output/ is git-ignored
REPORT_NAME = "result.json"
LOG_NAME = "run.log"
REPORT_SCHEMA_VERSION = 1
EXIT_MEANINGS = {
    EXIT_OK: "run complete",
    EXIT_CONFIG: "configuration invalid or unrecoverable error (see the log)",
    EXIT_ATTENTION: "run finished but needs your attention (see the warnings)",
}
CLI_WORKFLOW = "cli"
# The menu workflows of start_local.bat (local_launcher.py). Both are preparation only: the pipeline
# itself refuses them unless every setting below holds (workflow_guard), so a launcher mistake
# cannot turn one into an application run.
SCAN_AND_SCORE, FULL_PREPARATION = "scan-and-score", "full-preparation"
# Opt-in automatic applications (start_local.bat with launcher_workflow.json {"workflow": "auto-apply"}).
# The only launcher workflow that may submit: AUTO mode (still refused until one SAFE-mode application
# is confirmed), this run's validated scope only, Naukri job identity checked before Apply.
AUTO_APPLY = "auto-apply"
WORKFLOWS = (CLI_WORKFLOW, SCAN_AND_SCORE, FULL_PREPARATION, AUTO_APPLY)
PREPARATION_EXPORT_LIMIT, PREPARATION_WINDOW_HOURS = 2, 24
AUTO_APPLY_STAGES = ("scrape_jobs.py", "score_jobs.py", "tailor_job.py")
AUTO_APPLY_MAX_APPLICATIONS = 2  # upper bound only: APPLICATION_MAX_JOBS from .env is used as configured
AUTO_APPLY_MIN_SCORE = 8.0


def exit_meaning(code: Optional[int]) -> str:
    if code is None:
        return "unknown: the run ended without a final exit code"
    return EXIT_MEANINGS.get(code, "unexpected exit code")


def new_run_dir(base: Optional[Path] = None, now: Optional[datetime] = None) -> Path:
    """Create and return a new, empty run folder <base>/<YYYYmmddTHHMMSS>-<random>. mkdir without
    exist_ok is atomic, so two runs started in the same second never share (or overwrite) a folder."""
    base = Path(base or RUNS_DIR)
    stamp = (now or datetime.now()).strftime("%Y%m%dT%H%M%S")
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(100):
        candidate = base / f"{stamp}-{uuid.uuid4().hex[:6]}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"could not create a unique run folder in {base}")


def prepare_run_dir(path: Path) -> Path:
    """--run-dir: use this folder for run.log and result.json. Refused when it already holds either,
    so an earlier run's report or log is never overwritten."""
    path = Path(path)
    existing = [name for name in (REPORT_NAME, LOG_NAME) if (path / name).exists()]
    if existing:
        raise ValueError(f"{path} already holds {', '.join(existing)} from another run; use a new folder")
    path.mkdir(parents=True, exist_ok=True)
    return path


class _Tee(io.TextIOBase):
    """Writes to the console stream and the run log. The console never stops the log: characters the
    console cannot encode are replaced there, the log (UTF-8) keeps them."""

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, text):
        try:
            self.stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, "encoding", None) or "ascii"
            self.stream.write(text.encode(encoding, "replace").decode(encoding))
        if self.log is not None:
            self.log.write(text)
            self.log.flush()
        return len(text)

    def flush(self):
        self.stream.flush()
        if self.log is not None:
            self.log.flush()

    def isatty(self):
        return self.stream.isatty()

    @property
    def encoding(self):
        return getattr(self.stream, "encoding", "utf-8")


def streaming_agent_runner(argv, cwd=None, env=None, timeout=None, check=False):
    """subprocess.run replacement for run_agent_stages when a run log is kept: the Agent stage's
    stdout and stderr are copied line by line to sys.stdout (console + run.log) instead of going
    straight to the console, so stage errors are in the log. Same contract: returns an object with
    returncode, raises subprocess.TimeoutExpired after killing the stage."""
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.Popen(argv, cwd=cwd, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def pump():
        for raw in proc.stdout:
            # "\r\n" from a Windows child -> "\n": the text-mode console/log add their own line ending
            # (writing "\r\n" as is gave "\r\r\n" in run.log).
            sys.stdout.write(raw.decode("utf-8", "replace").replace("\r\n", "\n"))
        proc.stdout.close()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        reader.join(5)
        raise
    reader.join()
    return subprocess.CompletedProcess(argv, proc.returncode)


def logged_agent_stages(*args, **kwargs) -> AgentRunResult:
    """run_agent_stages (same scope rules, unchanged) with each stage's output copied into run.log."""
    return run_agent_stages(*args, runner=streaming_agent_runner, **kwargs)


def own_env_keys(env_file: Path = BASE_DIR / ".env") -> set:
    """Key names (never values) defined in this project's .env."""
    from dotenv import dotenv_values
    return set(dotenv_values(env_file)) if Path(env_file).is_file() else set()


def workflow_guard(workflow: str, args, settings: PipelineSettings, env=None,
                   project_env_keys: Optional[set] = None) -> list[str]:
    """Problems that make a start_local.bat workflow unsafe to run (empty = OK). The CLI workflow
    ('cli') has no extra rules. scan-and-score / full-preparation are preparation only and must never
    reach Apply; auto-apply must be exactly the bounded AUTO configuration (nothing looser)."""
    if workflow == CLI_WORKFLOW:
        return []
    env = os.environ if env is None else env
    keys = own_env_keys() if project_env_keys is None else project_env_keys
    problems = []
    if "artifact_sync" in keys:
        # ai_agent_bridge.agent_environment drops every key named in this project's .env before
        # starting the Agent, so artifact_sync=0 would never reach it.
        problems.append("remove artifact_sync from this project's .env (it would be dropped before the Agent "
                        "starts, so Drive mirroring could not be kept off)")
    if args.sync_only:
        problems.append("--sync-only is not a menu workflow")
    if workflow == AUTO_APPLY:
        if args.no_apply:
            problems.append("auto-apply cannot be combined with --no-apply")
        if settings.mode != AUTO:
            problems.append(f"APPLICATION_MODE must be AUTO for auto-apply (is {settings.mode})")
        if list(settings.agent_stages) != list(AUTO_APPLY_STAGES):
            problems.append(f"AI_AGENT_STAGES must be {','.join(AUTO_APPLY_STAGES)} for auto-apply "
                            f"(is {','.join(settings.agent_stages)})")
        if not 1 <= settings.max_applications <= AUTO_APPLY_MAX_APPLICATIONS:
            problems.append(f"APPLICATION_MAX_JOBS must be 1-{AUTO_APPLY_MAX_APPLICATIONS} for auto-apply "
                            f"(is {settings.max_applications})")
        if float(settings.score_threshold) < AUTO_APPLY_MIN_SCORE:
            problems.append(f"APPLICATION_SCORE_THRESHOLD must be at least {AUTO_APPLY_MIN_SCORE:g} for "
                            f"auto-apply (is {float(settings.score_threshold):g})")
    else:
        if not args.no_apply:
            problems.append("--no-apply is required (menu workflows never submit applications)")
        if settings.mode != SAFE:
            problems.append(f"APPLICATION_MODE must be SAFE (is {settings.mode})")
    if settings.sheets_enabled:
        problems.append("GOOGLE_SHEETS_ENABLED must be false (queued rows stay queued)")
    if (env.get("artifact_sync") or "").strip().lower() not in ("0", "false", "off", "no"):
        problems.append("artifact_sync must be 0 (no Agent artifact mirroring to Drive)")
    if settings.export_limit != PREPARATION_EXPORT_LIMIT:
        problems.append(f"AI_AGENT_EXPORT_LIMIT must be {PREPARATION_EXPORT_LIMIT} (is {settings.export_limit})")
    if settings.window_hours != PREPARATION_WINDOW_HOURS:
        problems.append(f"APPLICATION_JOB_WINDOW_HOURS must be {PREPARATION_WINDOW_HOURS} (is {settings.window_hours})")
    unsafe = [stage for stage in settings.agent_stages if stage not in SCOPE_SAFE_STAGES]
    if unsafe:
        problems.append(f"AI_AGENT_STAGES has stage(s) that cannot be scoped: {', '.join(unsafe)}")
    if workflow == SCAN_AND_SCORE and "tailor_job.py" in settings.agent_stages:
        problems.append("scan-and-score must not run tailor_job.py")
    return problems


def _known(value, known: bool):
    return value if known else None


def report_counts(s: RunSummary, *, no_apply: bool, sync_only: bool) -> dict:
    """RunSummary counts for result.json. A count from a step that did not run is None (unknown),
    never 0."""
    ran = not s.fatal and not sync_only
    scanned = ran and (s.scan_status == "OK" or s.scan_status.startswith("STOPPED"))
    agent_ran = ran and not s.dry_run and bool(s.exported)
    applied = ran and not no_apply and s.naukri_login == "OK"
    return {
        "scan": {key: _known(getattr(s, key), scanned) for key in (
            "jobs_scanned", "new_jobs", "duplicates_removed", "skipped_stale_cards",
            "skipped_experience_cards", "description_missing")},
        "freshness": {key: _known(getattr(s, key), ran) for key in ("fresh_jobs", "already_applied")},
        "export": {key: _known(getattr(s, key), ran) for key in (
            "discovered_unscored", "not_fresh_unscored", "export_experience_ineligible",
            "export_experience_review", "export_candidates", "export_rejected", "exported",
            "export_deferred", "export_limit")},
        "agent": {
            "newly_scored": _known(s.agent_new_scored, agent_ran),
            "newly_tailored": _known(s.agent_new_tailored, agent_ran),
            "other_jobs_scored": _known(s.agent_new_scored_other, agent_ran),
            "other_jobs_tailored": _known(s.agent_new_tailored_other, agent_ran),
            "new_in_agent_store": _known(s.agent_new_in_store, agent_ran),
            "exported_scored": _known(s.exported_scored, agent_ran),
            "exported_recommended": _known(s.exported_recommended, agent_ran),
        },
        "scores_imported": {key: _known(getattr(s, key), ran and not s.dry_run) for key in (
            "scores_imported", "recommended", "review", "skipped_scores")},
        "eligibility": {key: _known(getattr(s, key), ran) for key in (
            "candidates", "eligible", "experience_ineligible", "experience_review")},
        "applications": {key: _known(getattr(s, key), applied) for key in (
            "applied", "application_skipped", "failed", "recovery_required", "dry_run_ready")},
        "google_sheets": {"status": s.sheets.status, "rows_written": _known(s.sheets.synced, not s.fatal),
                          "pending": _known(s.sheets.pending, not s.fatal)},
    }


def report_stages(s: RunSummary, *, no_apply: bool, sync_only: bool) -> dict:
    """One status per pipeline step. Steps that never started say NOT RUN, not SKIPPED or OK."""
    if s.fatal:
        not_run = "NOT RUN (configuration invalid)"
        return {"configuration": "FAILED", "naukri_session": not_run, "scan": not_run, "export": not_run,
                "ai_agent": not_run, "applications": not_run, "google_sheets": not_run}
    if sync_only:
        not_run = "NOT RUN (--sync-only)"
        return {"configuration": "OK", "naukri_session": not_run, "scan": not_run, "export": not_run,
                "ai_agent": not_run, "applications": not_run, "google_sheets": s.sheets.status}
    if no_apply:
        applications = "SKIPPED (--no-apply)"
    elif s.naukri_login != "OK":
        applications = f"SKIPPED (Naukri login: {s.naukri_login})"
    else:
        applications = f"STOPPED ({s.stopped})" if s.stopped else "OK"
    export = ("SKIPPED (dry run)" if s.dry_run else
              f"{s.exported} exported, {s.export_deferred} deferred, {s.export_rejected} rejected")
    return {"configuration": "OK", "naukri_session": s.naukri_login, "scan": s.scan_status, "export": export,
            "ai_agent": s.agent_status, "applications": applications, "google_sheets": s.sheets.status}


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()  # local time with its UTC offset


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


class RunRecorder:
    """--run-dir: copies the console to run.log and keeps result.json current.

    result.json is written when the run starts (status "running", exit_code null) and replaced
    atomically when it ends, so a run that was killed still leaves an honest, incomplete report."""

    def __init__(self, run_dir: Path, workflow: str, options: dict, settings: PipelineSettings,
                 clock: Callable[[], datetime] = _now):
        self.run_dir = Path(run_dir)
        self.report_path, self.log_path = self.run_dir / REPORT_NAME, self.run_dir / LOG_NAME
        self.clock = clock
        self._log = None
        self._streams = None
        self.started = clock()
        self.report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "run_id": self.run_dir.name,
            "status": "running",
            "started_at": self.started.isoformat(),
            "finished_at": None,
            "duration_seconds": None,
            "workflow": workflow,
            "application_mode": settings.mode,
            "no_apply": bool(options.get("no_apply")),
            "options": options,
            "effective_settings": {
                "agent_stages": list(settings.agent_stages),
                "export_limit": settings.export_limit,
                "window_hours": settings.window_hours,
                "score_threshold": settings.score_threshold,
                "max_applications": settings.max_applications,
                "naukri_enabled": settings.naukri_enabled,
                "google_sheets_enabled": settings.sheets_enabled,
                "artifact_sync": os.environ.get("artifact_sync"),  # None = not set (Agent default: on)
                "agent_job_sources": "naukri",  # always set by ai_agent_bridge.agent_environment
            },
            "exit_code": None,
            "exit_meaning": None,
            "stages": None,
            "agent_export_id": None,
            "exported_job_ids": None,
            "agent_scope_report": None,
            "counts": None,
            "warnings": [],
            "fatal": None,
            "error": None,
            # auto-apply only (null otherwise, or when the step did not run)
            "scope_validation": None,
            "candidates_outside_run": None,
            "eligibility_results": None,
            "applications": None,
            "application_outcomes": None,
            "summary_text": None,
            "log_file": str(self.log_path),
            "report_file": str(self.report_path),
        }

    def start(self) -> None:
        self._log = open(self.log_path, "x", encoding="utf-8")  # "x": never append to another run's log
        self._streams = (sys.stdout, sys.stderr)
        sys.stdout, sys.stderr = _Tee(sys.stdout, self._log), _Tee(sys.stderr, self._log)
        print(f"Run {self.report['run_id']} ({self.report['workflow']}): log {self.log_path}")
        self.save()

    def save(self) -> None:
        write_json_atomic(self.report_path, self.report)

    def finish(self, status: str, exit_code: Optional[int], summary: Optional[RunSummary] = None,
               no_apply: bool = False, sync_only: bool = False, fatal: Optional[str] = None,
               error: Optional[str] = None, complete: bool = True) -> None:
        """complete=False (crash, Ctrl+C): the summary is partial, so only facts it already holds
        (export id, exported jobs, warnings) are recorded; stages and counts stay null (unknown)."""
        finished = self.clock()
        self.report.update(status=status, finished_at=finished.isoformat(),
                           duration_seconds=round((finished - self.started).total_seconds(), 1),
                           exit_code=exit_code, exit_meaning=exit_meaning(exit_code), fatal=fatal, error=error)
        if summary is not None:
            self.report.update(agent_export_id=summary.agent_export_id, warnings=list(summary.warnings),
                               fatal=summary.fatal or fatal)
            # Applications already decided are facts even in a partial run (crash/Ctrl+C afterwards).
            applications = summary.applications
            self.report.update(
                scope_validation=summary.scope_validation, candidates_outside_run=summary.candidates_outside_run,
                eligibility_results=summary.eligibility_results, applications=applications,
                application_outcomes=(dict(Counter(a["outcome"] for a in applications))
                                      if applications is not None else None))
            if summary.agent_export_id:
                self.report["exported_job_ids"] = list(summary.exported_ids)
            if complete:
                self.report.update(
                    stages=report_stages(summary, no_apply=no_apply, sync_only=sync_only),
                    exported_job_ids=(None if summary.fatal or sync_only or summary.dry_run
                                      else list(summary.exported_ids)),
                    agent_scope_report=summary.scope_report,
                    counts=report_counts(summary, no_apply=no_apply, sync_only=sync_only),
                    summary_text=format_summary(summary),
                )
        self.save()

    def close(self) -> None:
        if self._streams is not None:
            for tee in (sys.stdout, sys.stderr):
                if isinstance(tee, _Tee):
                    tee.flush()
                    tee.log = None  # logging handlers may still hold the tee: console only from now on
            sys.stdout, sys.stderr = self._streams
            self._streams = None
        if self._log is not None:
            self._log.close()
            self._log = None


def load_report(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """(report, None) or (None, problem). Read-only."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "no result.json"
    except (OSError, UnicodeDecodeError) as e:
        return None, f"unreadable: {type(e).__name__}"
    except ValueError as e:
        return None, f"not valid JSON: {e}"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    missing = [key for key in ("run_id", "started_at", "status") if not data.get(key)]
    if missing:
        return None, f"missing {', '.join(missing)}"
    return data, None


def find_last_run(runs_dir: Optional[Path] = None) -> tuple[Optional[dict], Optional[Path], list[tuple[Path, str]]]:
    """The newest readable report under runs_dir (folders are named by start time, newest last).
    Returns (report, its path, [(newer folder, problem), ...] skipped on the way). Read-only."""
    runs_dir = Path(runs_dir or RUNS_DIR)
    skipped = []
    if not Path(runs_dir).is_dir():
        return None, None, skipped
    folders = sorted((p for p in Path(runs_dir).iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for folder in folders:
        report, problem = load_report(folder / REPORT_NAME)
        if report is not None:
            return report, folder / REPORT_NAME, skipped
        skipped.append((folder, problem))
    return None, None, skipped


def format_last_run(report: dict, path: Path, skipped: list[tuple[Path, str]]) -> str:
    def value(key, default="unknown"):
        found = report.get(key)
        return default if found is None or found == "" else found

    exit_code = report.get("exit_code")
    lines = ["LAST RUN", "-" * 60]
    for folder, problem in skipped:
        lines.append(f"(skipped newer folder {folder.name}: {problem})")
    lines += [
        f"{'Run ID:':<22}{value('run_id')}",
        f"{'Status:':<22}{value('status')}",
        f"{'Started:':<22}{value('started_at')}",
        f"{'Finished:':<22}{value('finished_at', 'not recorded')}",
        f"{'Duration (s):':<22}{value('duration_seconds', 'not recorded')}",
        f"{'Workflow:':<22}{value('workflow')}",
        f"{'Mode:':<22}{value('application_mode')}{' (--no-apply)' if report.get('no_apply') else ''}",
        f"{'Exit code:':<22}{'not recorded' if exit_code is None else exit_code} ({exit_meaning(exit_code)})",
        f"{'Agent export ID:':<22}{value('agent_export_id', 'none (nothing exported, or unknown)')}",
        f"{'Exported job IDs:':<22}{report.get('exported_job_ids') if report.get('exported_job_ids') is not None else 'unknown'}",
    ]
    if report.get("status") == "running":
        lines.append("WARNING: this run never finished its report (killed or still running); treat it as failed.")
    stages = report.get("stages")
    if isinstance(stages, dict):
        lines += ["", "Stages:"] + [f"  {name:<20}{status}" for name, status in stages.items()]
    for label, key in (("Fatal", "fatal"), ("Error", "error")):
        if report.get(key):
            lines.append(f"{label}: {report[key]}")
    warnings = report.get("warnings") or []
    if warnings:
        lines += ["", "Warnings:"] + [f"  - {w}" for w in warnings]
    if report.get("summary_text"):
        lines += ["", report["summary_text"]]
    lines += ["", f"{'Report:':<22}{path}", f"{'Log:':<22}{value('log_file', path.parent / LOG_NAME)}"]
    return "\n".join(lines)


def show_last_run(runs_dir: Optional[Path] = None, out: Callable[[str], None] = print) -> int:
    """--last-run. Reads only: no database, browser, network, new folder or file."""
    runs_dir = Path(runs_dir or RUNS_DIR)
    report, path, skipped = find_last_run(runs_dir)
    if report is None:
        if skipped:
            out(f"No readable run report in {runs_dir}:")
            for folder, problem in skipped:
                out(f"  {folder.name}: {problem}")
            return EXIT_CONFIG
        out(f"No previous runs found in {runs_dir}. Runs started from start_local.bat (options 2 and 3) "
            "are recorded there.")
        return EXIT_OK
    out(format_last_run(report, path, skipped))
    return EXIT_OK


def _run_options(args) -> dict:
    """The pipeline flags of this run, for result.json (flags only; never environment values)."""
    return {key: getattr(args, key) for key in (
        "no_apply", "dry_run", "skip_scan", "skip_agent", "sync_only", "wait_for_login", "log_level")}


def main(argv: Optional[list[str]] = None, settings: Optional[PipelineSettings] = None,
         pipeline_factory: Optional[Callable[..., "ApplicationPipeline"]] = None) -> int:
    parser = argparse.ArgumentParser(description="Naukri Auto Job Apply: one local run of the full workflow")
    parser.add_argument("--check-config", action="store_true",
                        help="validate configuration and exit (read-only: never creates or changes the database)")
    parser.add_argument("--last-run", action="store_true",
                        help=f"show the newest run report under {RUNS_DIR} and exit (read-only)")
    parser.add_argument("--run-dir", type=Path, default=None,
                        help="record this run in DIR: run.log (console copy) and result.json (structured report)")
    parser.add_argument("--workflow", choices=WORKFLOWS, default=CLI_WORKFLOW,
                        help="workflow label for result.json; the start_local.bat workflows are preparation only "
                             "and are refused unless --no-apply, SAFE, Sheets off and artifact_sync=0 all hold")
    parser.add_argument("--dry-run", action="store_true",
                        help="open and check jobs but never click Apply, export, run the AI Agent or write Sheets")
    parser.add_argument("--no-apply", action="store_true", help="stop after eligibility (no application browser)")
    parser.add_argument("--skip-scan", action="store_true", help="use the jobs already stored in SQLite")
    parser.add_argument("--skip-agent", action="store_true", help="do not run the AI Agent (scores are still imported)")
    parser.add_argument("--sync-only", action="store_true", help="only retry pending Google Sheets synchronization")
    parser.add_argument("--eligibility-check", action="store_true",
                        help="report the experience check and eligibility of stored jobs; no browser, export, "
                             "AI Agent, Sheets or application")
    parser.add_argument("--wait-for-login", action="store_true",
                        help="if the Naukri session check finds the login page, wait in the visible browser "
                             f"(NAUKRI_LOGIN_WAIT_MINUTES, {NAUKRI_LOGIN_WAIT_MINUTES} min) for a manual login, "
                             "once, then continue; nothing is typed for you")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args(argv)
    if args.last_run:
        return show_last_run()
    if args.run_dir is not None and (args.check_config or args.eligibility_check):
        parser.error("--run-dir records pipeline runs; it cannot be combined with --check-config or --eligibility-check")
    if args.workflow != CLI_WORKFLOW and args.run_dir is None:
        parser.error(f"--workflow {args.workflow} requires --run-dir")
    recorder = None
    settings = settings or PipelineSettings()
    if args.run_dir is not None:
        try:
            run_dir = prepare_run_dir(args.run_dir)
        except (ValueError, OSError) as e:
            print(f"[FAIL] --run-dir: {e}", file=sys.stderr)
            return EXIT_CONFIG
        recorder = RunRecorder(run_dir, args.workflow, _run_options(args), settings)
        recorder.start()  # before logging is configured, so log records go to run.log too
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    if args.check_config:
        checks = validate_configuration(settings, initialize_db=False)
        print_configuration(checks)
        return EXIT_OK if configuration_ok(checks) else EXIT_CONFIG
    if args.eligibility_check:
        run_eligibility_check(settings)
        return EXIT_OK
    try:
        return _run_pipeline(args, settings, recorder, pipeline_factory or ApplicationPipeline)
    finally:
        if recorder is not None:
            recorder.close()
            print(f"Run report: {recorder.report_path}\nRun log:    {recorder.log_path}")


def _run_pipeline(args, settings: PipelineSettings, recorder: Optional[RunRecorder], pipeline_factory) -> int:
    problems = workflow_guard(args.workflow, args, settings)
    if problems:
        fatal = f"workflow {args.workflow} refused: " + "; ".join(problems)
        print(f"[FAIL] {fatal}. Nothing was run.")
        if recorder is not None:
            recorder.finish("failed", EXIT_CONFIG, fatal=fatal)
        return EXIT_CONFIG
    agent_options = {"agent_runner": logged_agent_stages} if recorder is not None else {}
    pipeline = None
    try:
        pipeline = pipeline_factory(settings, dry_run=args.dry_run, skip_scan=args.skip_scan,
                                    skip_agent=args.skip_agent, no_apply=args.no_apply, sync_only=args.sync_only,
                                    login_wait_minutes=NAUKRI_LOGIN_WAIT_MINUTES if args.wait_for_login else 0,
                                    auto_apply=args.workflow == AUTO_APPLY,  # passed workflow_guard above
                                    **agent_options)
        summary = pipeline.run()
    except KeyboardInterrupt:
        print("\nInterrupted. Everything already recorded stays in SQLite; an interrupted application "
              "is detected and sent to recovery on the next run.")
        if recorder is not None:
            recorder.finish("interrupted", EXIT_ATTENTION, pipeline and pipeline.summary, args.no_apply,
                            args.sync_only, error="interrupted (Ctrl+C) before the run finished", complete=False)
        return EXIT_ATTENTION
    except BaseException as e:
        if recorder is not None:
            traceback.print_exc()  # into run.log; the exception is re-raised unchanged
            recorder.finish("crashed", EXIT_CONFIG, pipeline and pipeline.summary, args.no_apply, args.sync_only,
                            error=f"{type(e).__name__}: {e}", complete=False)
        raise
    print("\n" + format_summary(summary))
    code = summary.exit_code
    if recorder is not None:
        status = {EXIT_OK: "complete", EXIT_ATTENTION: "attention"}.get(code, "failed")
        recorder.finish(status, code, summary, args.no_apply, args.sync_only)
    return code


if __name__ == "__main__":
    sys.exit(main())
