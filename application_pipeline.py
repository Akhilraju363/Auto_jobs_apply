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
import logging
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from config import (
    AI_AGENT_DIR,
    AI_AGENT_EXPORT_LIMIT,
    AI_AGENT_OUTPUT_DIR,
    AI_AGENT_PYTHON,
    AI_AGENT_STAGES,
    AI_AGENT_TIMEOUT_MINUTES,
    APPLICATION_JOB_WINDOW_HOURS,
    APPLICATION_MAX_JOBS,
    APPLICATION_MODE,
    APPLICATION_SCORE_THRESHOLD,
    CHROME_USER_DATA,
    DB_PATH,
    GOOGLE_SHEET_ID,
    GOOGLE_SHEET_TAB,
    GOOGLE_SHEETS_ENABLED,
    MASTER_RESUME_PATH,
    NAUKRI_ENABLED,
    NAUKRI_EXPORT_PATH,
    PREFERRED_LOCATIONS,
)
from ai_agent_bridge import (
    AgentRunResult,
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
    get_jobs,
    has_successful_application,
    init_db,
    queue_sheet_sync,
    save_experience_check,
)
from eligibility import FRESH, Eligibility, evaluate_job, job_freshness, priority_score
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


def validate_configuration(settings: PipelineSettings) -> list[ConfigCheck]:
    """Everything the pipeline needs, checked up front. Never creates the browser profile."""
    checks = []
    mode_ok = settings.mode in MODES
    checks.append(ConfigCheck("APPLICATION_MODE", mode_ok,
                              settings.mode if mode_ok else f"{settings.mode!r} is not SAFE or AUTO"))
    try:
        init_db()
        checks.append(ConfigCheck("SQLite database", True, str(DB_PATH)))
        confirmed = count_successful_applications()
    except Exception as e:  # noqa: BLE001 -- reported, the run stops
        checks.append(ConfigCheck("SQLite database", False, f"{type(e).__name__}: {e}"))
        confirmed = 0
    if settings.mode == AUTO:
        checks.append(ConfigCheck(
            "AUTO mode readiness", confirmed > 0,
            f"{confirmed} confirmed application(s) in history" if confirmed else
            "AUTO is refused until one SAFE-mode application has been confirmed end to end "
            "(set APPLICATION_MODE=SAFE and apply to one job first)"))
    checks.append(ConfigCheck("APPLICATION_JOB_WINDOW_HOURS", settings.window_hours > 0, f"{settings.window_hours}h"))
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
    experience check against the master resume."""
    artifact, _, _ = resolve_verified_resume(job["url"], agent_output_dir or settings.agent_output_dir)
    return evaluate_job(
        dict(job, history_applied=has_successful_application(job)),
        reference_time=reference_time,
        preferred_locations=settings.preferred_locations,
        applicant_experience=verified if verified is not None else verified_experience(settings),
        score_threshold=settings.score_threshold,
        resume_ready=artifact is not None and artifact.verified,
        window_hours=settings.window_hours,
    )


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
    scan_status: str = "SKIPPED"
    fresh_jobs: int = 0
    already_applied: int = 0
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

    def _experience_rejected(self, job: dict, status: str, reason: str) -> None:
        if status == INELIGIBLE:
            self.summary.experience_ineligible += 1
        elif status == REVIEW_REQUIRED:
            self.summary.experience_review += 1
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
            if job.get("application_state") in FINAL_APPLICATION_STATES and not already_applied(job):
                continue  # external / unavailable: nothing left to do
            if job_freshness(job, reference_time=reference, window_hours=self.settings.window_hours)[0] != FRESH:
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
                self._experience_rejected(job, check.status, check.reason)
        return pending

    def export_and_score(self, fresh_pending: list[dict]) -> None:
        s = self.summary
        wanted = {job["id"] for job in fresh_pending}
        if self.dry_run:
            s.exported = min(len(fresh_pending), self.settings.export_limit)
            s.agent_status = "SKIPPED (dry run)"
        else:
            export = export_discovered_jobs(self.settings.export_path, self.settings.export_limit,
                                            job_filter=lambda job: job["id"] in wanted)
            s.exported = len(export.exported)
            if self.skip_agent:
                s.agent_status = "SKIPPED (--skip-agent)"
            elif not export.exported:
                s.agent_status = "SKIPPED (no fresh unscored jobs)"
            else:
                result = self.agent_runner(self.settings.agent_dir, self.settings.agent_stages,
                                           self.settings.export_path, self.settings.agent_python,
                                           self.settings.agent_timeout_minutes)
                s.agent_status = "OK" if result.ok else f"{result.code}: {result.message}"
                if not result.ok:
                    s.warnings.append(f"AI Agent: {result.code}: {result.message}")
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

    def eligibility(self) -> list[dict]:
        reference = self.clock()
        eligible = []
        for job in get_application_candidates():
            self.summary.candidates += 1
            result = evaluate_candidate(job, self.settings, reference, verified=self.verified)
            save_experience_check(job["id"], result.experience_check)
            if result.eligible:
                eligible.append((job, result))
            elif result.experience_status != ELIGIBLE:
                self._experience_rejected(job, result.experience_status, result.reason)
            else:
                self.out(f"  [{job['id']}] {job['title']} @ {job['company']}: not eligible: {result.reason}")
        eligible.sort(key=lambda item: (-priority_score(item[0], item[1]), item[0]["id"]))
        self.summary.eligible = len(eligible)
        return [job for job, _ in eligible]

    def apply(self, eligible: list[dict]) -> None:
        s = self.summary
        batch = eligible[: self.settings.max_applications]
        if not batch:
            return
        self.out(f"\n{'DRY RUN - ' if self.dry_run else ''}{s.mode} mode: {len(batch)} application(s) this run"
                 + ("" if s.mode == AUTO else "; each one needs your exact 'y' or 'yes' before Apply is clicked"))
        answer = mode_prompt(s.mode, self.prompt, self.out)
        try:
            with self.browser_factory() as browser:
                runner = self.runner_factory(browser, answer, self.out, self.dry_run)
                for job in batch:
                    try:
                        result = runner.process(job)
                    except Exception as e:  # ApplicationStop (login, CAPTCHA, quit) or a browser failure
                        s.stopped = f"{getattr(e, 'code', type(e).__name__)}: {e}"
                        self._track(job)  # the runner already persisted whatever state it reached
                        break
                    self._count(result)
                    self._track(job)
        except Exception as e:  # noqa: BLE001 -- the browser could not start
            s.stopped = f"{getattr(e, 'code', type(e).__name__)}: {e}"
        if s.stopped:
            s.warnings.append(f"Applications stopped: {s.stopped}")

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


def format_summary(s: RunSummary) -> str:
    wide, thin = "=" * 60, "-" * 60

    def row(label: str, value) -> str:
        return f"{label + ':':<26}{value:>8}" if isinstance(value, int) else f"{label + ':':<26}{value}"

    sheet_status = s.sheets.status
    lines = [wide, "         NAUKRI AUTO JOB APPLICATION", wide, "",
             row("Mode", s.mode + (" (DRY RUN)" if s.dry_run else "")),
             row("Naukri login", s.naukri_login),
             row("Naukri scan", s.scan_status),
             row("Jobs scanned", s.jobs_scanned),
             row("New jobs stored", s.new_jobs),
             row("Duplicates removed", s.duplicates_removed),
             row(f"Fresh jobs (<{s.window_hours}h)", s.fresh_jobs),
             row("Already applied", s.already_applied),
             row("Would export (dry run)" if s.dry_run else "Exported to AI Agent", s.exported),
             "", "AI SCORING", thin,
             row("AI Agent", s.agent_status),
             row("Recommended", s.recommended),
             row("Review", s.review),
             row("Skipped", s.skipped_scores),
             "", "ELIGIBILITY", thin,
             row("Recommended candidates", s.candidates),
             row("Experience: skipped", s.experience_ineligible),
             row("Experience: review", s.experience_review),
             row("Eligible", s.eligible),
             "", "APPLICATIONS", thin]
    if s.dry_run:
        lines.append(row("Ready (dry run)", s.dry_run_ready))
    lines += [row("Applied", s.applied),
              row("Skipped", s.application_skipped),
              row("Failed", s.failed),
              row("Recovery required", s.recovery_required),
              "", "GOOGLE SHEETS", thin,
              row("Status", sheet_status),
              row("Synced", s.sheets.synced),
              row("Pending sync", s.sheets.pending)]
    if s.fatal or s.warnings:
        lines += ["", "ATTENTION", thin] + [f"- {w}" for w in ([s.fatal] if s.fatal else []) + s.warnings]
    if s.recovery_required:
        lines.append("- Recover with: python main.py --action application-status")
    status = "RUN FAILED" if s.fatal else ("RUN COMPLETE (ATTENTION NEEDED)" if s.exit_code else "RUN COMPLETE")
    lines += ["", wide, status, wide]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Naukri Auto Job Apply: one local run of the full workflow")
    parser.add_argument("--check-config", action="store_true", help="validate configuration and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="open and check jobs but never click Apply, export, run the AI Agent or write Sheets")
    parser.add_argument("--no-apply", action="store_true", help="stop after eligibility (no application browser)")
    parser.add_argument("--skip-scan", action="store_true", help="use the jobs already stored in SQLite")
    parser.add_argument("--skip-agent", action="store_true", help="do not run the AI Agent (scores are still imported)")
    parser.add_argument("--sync-only", action="store_true", help="only retry pending Google Sheets synchronization")
    parser.add_argument("--eligibility-check", action="store_true",
                        help="report the experience check and eligibility of stored jobs; no browser, export, "
                             "AI Agent, Sheets or application")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    settings = PipelineSettings()
    if args.check_config:
        checks = validate_configuration(settings)
        print_configuration(checks)
        return EXIT_OK if configuration_ok(checks) else EXIT_CONFIG
    if args.eligibility_check:
        run_eligibility_check(settings)
        return EXIT_OK
    pipeline = ApplicationPipeline(settings, dry_run=args.dry_run, skip_scan=args.skip_scan,
                                   skip_agent=args.skip_agent, no_apply=args.no_apply, sync_only=args.sync_only)
    try:
        summary = pipeline.run()
    except KeyboardInterrupt:
        print("\nInterrupted. Everything already recorded stays in SQLite; an interrupted application "
              "is detected and sent to recovery on the next run.")
        return EXIT_ATTENTION
    print("\n" + format_summary(summary))
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
