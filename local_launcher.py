#!/usr/bin/env python3
"""start_local.bat's launcher.

Default (double-click start_local.bat): AutoRun, the automatic scan-and-score run -- exactly menu
option 2, without the menu or a confirmation. `python local_launcher.py --menu` opens the developer
menu: configuration check, scan and score, full preparation (YES), last run, exit.

No launcher path submits an application. Every pipeline run started here gets --no-apply and
APPLICATION_MODE=SAFE, and application_pipeline.workflow_guard refuses the run on its own if any of
the preparation settings below is missing, so a mistake here cannot reach the Apply step.

Settings are enforced through the child process environment only. .env is never edited, and
python-dotenv (both projects) never overrides a variable that is already set, so these win:

    APPLICATION_MODE=SAFE              never AUTO
    artifact_sync=0                    the AI Agent does not mirror raw/scored/tailored JSON to Drive
    GOOGLE_SHEETS_ENABLED=false        no Sheets sync; queued rows stay queued (DISABLED keeps them)
    JOB_SOURCES=naukri                 no LinkedIn/Apify scrape (ai_agent_bridge sets it as well)
    AI_AGENT_EXPORT_LIMIT=2            at most 2 jobs scored/tailored per run
    APPLICATION_JOB_WINDOW_HOURS=24    the 24-hour freshness rule

Developers can still run application_pipeline.py directly; see docs/PIPELINE.md.
"""

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

PROJECT_DIR = Path(__file__).resolve().parent
PIPELINE_SCRIPT = PROJECT_DIR / "application_pipeline.py"
LOCK_NAME = ".launcher.lock"

SAFETY_ENV = {
    "APPLICATION_MODE": "SAFE",
    "artifact_sync": "0",
    "GOOGLE_SHEETS_ENABLED": "false",
    "JOB_SOURCES": "naukri",
    "AI_AGENT_EXPORT_LIMIT": "2",
    "APPLICATION_JOB_WINDOW_HOURS": "24",
}
SCAN_AND_SCORE_STAGES = ("scrape_jobs.py", "score_jobs.py")
REQUIRED_AGENT_SCRIPTS = ("scrape_jobs.py", "score_jobs.py", "tailor_job.py", "job_scope.py", "artifacts.py")
REQUIRED_PACKAGES = ("playwright", "dotenv", "pypdf")
# The Agent's artifacts.py must honour artifact_sync, or option 2/3 could still mirror files to Drive.
ARTIFACT_SYNC_MARKER = 'os.environ.get("artifact_sync"'

EXIT_OK, EXIT_CONFIG, EXIT_ATTENTION = 0, 1, 2

MENU = """
============================================================
  NAUKRI AUTO JOB APPLY - LOCAL LAUNCHER
  APPLICATION_MODE=SAFE. No option submits an application.
============================================================
  1  Configuration check         read-only
  2  Scan and score only         live Naukri scan + AI scoring
  3  Full preparation            scan + score + tailor resumes
  4  View previous run           read-only
  5  Exit
"""

SCAN_AND_SCORE_WARNING = """
SCAN AND SCORE ONLY - this will:
  - open Chrome with your saved Naukri profile (chrome_user_data) and run a LIVE Naukri scan
  - write discovered jobs and experience checks to the SQLite database (data/naukri_auto_apply.db)
    and rewrite exports/naukri_jobs.json and exports/agent_job_scope.json
  - run the AI Agent's scrape_jobs.py (merges the Naukri handoff into its output/raw_jobs.json; no
    LinkedIn/Apify scrape) and score_jobs.py, which calls the Agent's configured scoring LLM for at
    most 2 exported jobs and updates its output/scored_jobs.json
It will NOT tailor resumes, upload anything to Google Drive (artifact_sync=0), sync Google Sheets
(queued rows stay queued) or submit any application (--no-apply, SAFE).
"""

FULL_PREPARATION_WARNING = """
FULL PREPARATION (NO SUBMISSION) - this will:
  - open Chrome with your saved Naukri profile (chrome_user_data) and run a LIVE Naukri scan
  - write discovered jobs and experience checks to the SQLite database (data/naukri_auto_apply.db)
    and rewrite exports/naukri_jobs.json and exports/agent_job_scope.json
  - run the AI Agent stages {stages}:
      scoring calls the Agent's configured LLM for at most 2 exported jobs;
      tailoring calls the LLM again for each of those jobs that scores 8+ and writes a resume to the
      Agent's output/generated_resumes and output/tailored_jobs.json
  - when the Agent has google_drive_folder_id configured, tailoring also creates a Google Drive
    folder per job, a temporary Google Doc to render the PDF, and UPLOADS THE RESUME PDF TO DRIVE
LLM use may cost money or free-tier quota, depending on the AI Agent's LLM configuration.
Disabled for this run: the Agent's raw/scored/tailored JSON mirroring to Drive (artifact_sync=0) and
Google Sheets sync (queued rows stay queued). No application is submitted (--no-apply, SAFE).
"""


class WorkflowBlocked(Exception):
    """A menu workflow cannot be run safely with the current configuration."""


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    blocks: tuple = ()  # menu choices this failure blocks ("2", "3")


@dataclass(frozen=True)
class Workflow:
    choice: str
    workflow: str  # application_pipeline --workflow value
    title: str
    args: tuple  # pipeline arguments, without --run-dir
    env: dict = field(default_factory=dict)  # on top of SAFETY_ENV
    confirmation: str = "y"  # "y": y/yes; "YES": exactly YES
    warning: str = ""


def child_environment(overrides: dict, base: Optional[dict] = None) -> dict:
    """The process environment for a pipeline started from the menu: the caller's environment, then
    SAFETY_ENV, then the workflow's own settings. Nothing is written to .env."""
    env = dict(os.environ if base is None else base)
    env.update(SAFETY_ENV)
    env.update(overrides)
    return env


def build_workflow(choice: str, configured_stages: list) -> Workflow:
    """Options 2 and 3 as data. Raises WorkflowBlocked when the stages cannot be kept in scope or
    (option 2) would tailor."""
    from ai_agent_bridge import SCOPE_SAFE_STAGES

    if choice == "2":
        stages = list(SCAN_AND_SCORE_STAGES)
        workflow = Workflow("2", "scan-and-score", "Scan and score only",
                            ("--no-apply", "--workflow", "scan-and-score"),
                            {"AI_AGENT_STAGES": ",".join(stages)}, "y", SCAN_AND_SCORE_WARNING)
    elif choice == "3":
        stages = [str(s).strip() for s in configured_stages if str(s).strip()]
        if not stages:
            raise WorkflowBlocked("AI_AGENT_STAGES is empty")
        workflow = Workflow("3", "full-preparation", "Full preparation (no submission)",
                            ("--no-apply", "--workflow", "full-preparation"),
                            {"AI_AGENT_STAGES": ",".join(stages)}, "YES",
                            FULL_PREPARATION_WARNING.format(stages=", ".join(stages)))
    else:
        raise WorkflowBlocked(f"option {choice!r} is not a pipeline workflow")
    unsafe = [stage for stage in stages if stage not in SCOPE_SAFE_STAGES]
    if unsafe:
        raise WorkflowBlocked(f"AI_AGENT_STAGES has stage(s) that cannot be limited to this run's exported jobs: "
                              f"{', '.join(unsafe)} (allowed: {', '.join(SCOPE_SAFE_STAGES)})")
    if choice == "2" and "tailor_job.py" in stages:
        raise WorkflowBlocked("scan and score must not run tailor_job.py")
    ensure_cannot_submit(workflow)
    return workflow


def ensure_cannot_submit(workflow: Workflow) -> None:
    """Every menu pipeline run is --no-apply, SAFE, Sheets off and artifact sync off."""
    env = child_environment(workflow.env, base={})
    if "--no-apply" not in workflow.args or "--sync-only" in workflow.args:
        raise WorkflowBlocked(f"{workflow.title}: --no-apply is missing; refusing to run")
    expected = {"APPLICATION_MODE": "SAFE", "GOOGLE_SHEETS_ENABLED": "false", "artifact_sync": "0"}
    wrong = [key for key, value in expected.items() if env.get(key) != value]
    if wrong:
        raise WorkflowBlocked(f"{workflow.title}: unsafe setting(s) {', '.join(wrong)}; refusing to run")


def preflight_checks(agent_dir: Optional[Path], agent_output_dir: Optional[Path], agent_python: Optional[Path],
                     configured_stages: list, project_dir: Path = PROJECT_DIR) -> list[Check]:
    """Read-only checks of everything options 2 and 3 depend on (files and folders only: no database,
    browser or network)."""
    from ai_agent_bridge import SCOPE_SAFE_STAGES, resolve_agent_python

    live = ("2", "3", "auto")  # menu options 2/3 and the auto-apply workflow (AUTO_APPLY_CHOICE)
    checks = []
    version_ok = sys.version_info >= (3, 11)
    checks.append(Check("Python", version_ok, f"{sys.version.split()[0]} ({sys.executable})",
                        () if version_ok else live))
    missing = [name for name in REQUIRED_PACKAGES if importlib.util.find_spec(name) is None]
    checks.append(Check("Packages", not missing, "playwright, python-dotenv, pypdf" if not missing else
                        f"missing: {', '.join(missing)} (pip install -r requirements.txt)", live if missing else ()))
    env_file = project_dir / ".env"
    checks.append(Check(".env", env_file.is_file(), str(env_file) if env_file.is_file() else
                        f"{env_file} not found (copy .env.example)", () if env_file.is_file() else live))

    agent_ok = bool(agent_dir) and Path(agent_dir).is_dir()
    absent = [s for s in REQUIRED_AGENT_SCRIPTS if not (Path(agent_dir) / "scripts" / s).is_file()] if agent_ok else []
    if not agent_dir:
        checks.append(Check("AI_AGENT_DIR", False, "not set in .env", live))
    elif not agent_ok or absent:
        checks.append(Check("AI_AGENT_DIR", False, f"{agent_dir} is not the AI Agent checkout"
                            + (f" (no scripts/{', scripts/'.join(absent)})" if absent else ""), live))
    else:
        checks.append(Check("AI_AGENT_DIR", True, str(agent_dir)))

    expected_output = Path(agent_dir) / "output" if agent_dir else None
    if not agent_output_dir:
        checks.append(Check("AI_AGENT_OUTPUT_DIR", False, "not set in .env", live))
    elif expected_output is None or Path(agent_output_dir).resolve() != expected_output.resolve():
        expected = expected_output or "the output folder inside AI_AGENT_DIR"
        checks.append(Check("AI_AGENT_OUTPUT_DIR", False, f"{agent_output_dir} is not {expected}", live))
    elif not Path(agent_output_dir).is_dir():
        checks.append(Check("AI_AGENT_OUTPUT_DIR", False, f"{agent_output_dir} does not exist", live))
    else:
        checks.append(Check("AI_AGENT_OUTPUT_DIR", True, str(agent_output_dir)))

    python = resolve_agent_python(Path(agent_dir), agent_python) if agent_ok else None
    checks.append(Check("AI Agent Python", python is not None, str(python) if python else
                        "not found (AI_AGENT_DIR\\.venv or AI_AGENT_PYTHON)", () if python else live))

    artifacts = Path(agent_dir) / "scripts" / "artifacts.py" if agent_ok else None
    try:
        honours = artifacts is not None and ARTIFACT_SYNC_MARKER in artifacts.read_text(encoding="utf-8")
    except OSError:
        honours = False
    checks.append(Check("Agent Drive mirroring off", honours,
                        "artifacts.py honours artifact_sync=0" if honours else
                        "cannot confirm the Agent's artifacts.py honours artifact_sync=0: Drive mirroring "
                        "could not be guaranteed off", () if honours else live))
    # ai_agent_bridge drops every key named in this project's .env from the Agent's environment, so an
    # artifact_sync entry there would silently undo artifact_sync=0. Key names only; values are never read.
    from application_pipeline import own_env_keys
    shadowed = "artifact_sync" in own_env_keys(env_file)
    checks.append(Check("artifact_sync not in this .env", not shadowed,
                        "not defined (the launcher's artifact_sync=0 reaches the Agent)" if not shadowed else
                        "artifact_sync is defined in this project's .env: the bridge would drop the launcher's "
                        "artifact_sync=0; remove that line", live if shadowed else ()))

    stages = [str(s).strip() for s in configured_stages if str(s).strip()]
    unsafe = [s for s in stages if s not in SCOPE_SAFE_STAGES]
    checks.append(Check("Option 3 stages (AI_AGENT_STAGES)", bool(stages) and not unsafe,
                        ", ".join(stages) if stages and not unsafe else
                        (f"cannot be scoped: {', '.join(unsafe)}" if unsafe else "empty"),
                        () if stages and not unsafe else ("3",)))
    checks.append(Check("Option 2 stages", True, ", ".join(SCAN_AND_SCORE_STAGES) + " (no tailoring)"))
    return checks


def blocked_choices(checks: list[Check]) -> dict:
    """{choice: [reason, ...]} for options 2/3 that a failed check blocks."""
    blocked = {}
    for check in checks:
        for choice in check.blocks:
            blocked.setdefault(choice, []).append(f"{check.name}: {check.detail}")
    return blocked


def print_checks(checks: list[Check], out: Callable[[str], None]) -> None:
    out("LAUNCHER CHECKS (read-only)")
    for check in checks:
        mark = "OK  " if check.ok else ("FAIL" if check.blocks else "WARN")
        out(f"  [{mark}] {check.name:<34} {check.detail}")


class LauncherLock:
    """One menu pipeline run at a time. An OS byte-range lock on output/runs/.launcher.lock: the OS
    drops it when the process ends, so a crash never leaves a permanent lock (the empty file stays)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None


def launch_pipeline(args: list, env: dict, out: Callable[[str], None] = print) -> int:
    """Run application_pipeline.py with this launcher's Python and return its real exit code. Ctrl+C
    reaches the pipeline too; we wait for it to record its report instead of killing it."""
    sys.stdout.flush()  # our text before the child's, also when stdout is a pipe
    process = subprocess.Popen([sys.executable, str(PIPELINE_SCRIPT), *args], cwd=str(PROJECT_DIR), env=env)
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            out("Waiting for the pipeline to stop and write its report...")


def report_after_run(run_dir: Path, returncode: int, out: Callable[[str], None]) -> None:
    """The real exit code plus what the run's own report says. A missing, unfinished or disagreeing
    report is called out; nothing is reported as successful unless the process exited 0."""
    from application_pipeline import LOG_NAME, REPORT_NAME, exit_meaning, load_report

    report, problem = load_report(run_dir / REPORT_NAME)
    out("")
    out("=" * 60)
    out(f"Exit code:        {returncode} ({exit_meaning(returncode)})")
    out(f"Run ID:           {run_dir.name}")
    if report is None:
        out(f"Report:           NOT WRITTEN ({problem}): treat this run as failed")
    else:
        export_id = report.get("agent_export_id")
        out(f"Agent export ID:  {export_id or 'none (nothing exported this run)'}")
        if report.get("status") == "running":
            out("WARNING: the pipeline ended without finishing its report: treat this run as failed.")
        elif report.get("exit_code") != returncode:
            out(f"WARNING: the report says exit code {report.get('exit_code')} but the process exited "
                f"{returncode}: the process exit code is authoritative.")
        out(f"Status:           {report.get('status')}")
        applications = report.get("applications")
        if isinstance(applications, list):  # auto-apply runs
            out(f"Applications:     {report.get('application_outcomes') or 'none attempted'}")
            for record in applications:
                out(f"  [{record.get('job_id')}] {record.get('outcome')}: {record.get('title') or '?'} @ "
                    f"{record.get('company') or '?'}" + (f" ({record.get('reason')})" if record.get("reason") else ""))
    out(f"Report file:      {run_dir / REPORT_NAME}")
    out(f"Log file:         {run_dir / LOG_NAME}")
    out("RUN COMPLETE" if returncode == EXIT_OK else "RUN DID NOT COMPLETE SUCCESSFULLY")
    out("=" * 60)


def confirmed(workflow: Workflow, input_fn: Callable[[str], str]) -> bool:
    if workflow.confirmation == "YES":
        return input_fn("Type YES (capitals) to continue; anything else cancels: ") == "YES"
    return input_fn("Type y to start; anything else cancels: ").strip().lower() in ("y", "yes")


@dataclass
class Launcher:
    checks: list
    configured_stages: list
    runs_dir: Path
    input_fn: Callable[[str], str] = input
    out: Callable[[str], None] = print
    launch: Callable[..., int] = launch_pipeline
    new_run_dir: Optional[Callable[[Path], Path]] = None
    last_exit: int = EXIT_OK

    def run(self) -> int:
        while True:
            self.out(MENU)
            try:
                choice = self.input_fn("Choose 1-5: ").strip()
            except (EOFError, KeyboardInterrupt):
                self.out("\nNo choice made; exiting without running anything.")
                return self.last_exit
            if choice == "5":
                self.out("Exit: nothing was run.")
                return self.last_exit
            if choice not in ("1", "2", "3", "4"):
                self.out(f"Invalid choice {choice!r}: type a number from 1 to 5. Nothing was run.")
                continue
            try:
                self.handle(choice)
            except (EOFError, KeyboardInterrupt):
                self.out("\nCancelled; nothing further was run.")
                return self.last_exit
            try:
                self.input_fn("\nPress Enter to return to the menu...")
            except (EOFError, KeyboardInterrupt):
                return self.last_exit

    def handle(self, choice: str) -> None:
        if choice == "1":
            print_checks(self.checks, self.out)
            self.out("")
            code = self.launch(["--check-config"], child_environment({}))
            failed = any(c.blocks and not c.ok for c in self.checks)
            self.last_exit = EXIT_CONFIG if failed and code == EXIT_OK else code
            self.out(f"\nConfiguration check exit code: {self.last_exit}"
                     + (" (launcher checks failed: see FAIL above)" if failed else ""))
        elif choice == "4":
            self.last_exit = self.launch(["--last-run"], child_environment({}))
        else:
            self.last_exit = self.run_workflow(choice)

    def run_workflow(self, choice: str) -> int:
        reasons = blocked_choices(self.checks).get(choice)
        if reasons:
            self.out(f"Option {choice} is blocked; nothing was run:")
            for reason in reasons:
                self.out(f"  - {reason}")
            self.out("Run option 1 for the full configuration check.")
            return EXIT_CONFIG
        try:
            workflow = build_workflow(choice, self.configured_stages)
        except WorkflowBlocked as e:
            self.out(f"Option {choice} is blocked; nothing was run: {e}")
            return EXIT_CONFIG
        self.out(workflow.warning)
        if not confirmed(workflow, self.input_fn):
            self.out("Cancelled; nothing was run.")
            return self.last_exit
        lock = LauncherLock(self.runs_dir / LOCK_NAME)
        if not lock.acquire():
            self.out("Another launcher run is in progress (output\\runs\\.launcher.lock is held); nothing was run.")
            return EXIT_CONFIG
        try:
            if self.new_run_dir is None:
                from application_pipeline import new_run_dir
                run_dir = new_run_dir(self.runs_dir)
            else:
                run_dir = self.new_run_dir(self.runs_dir)
            self.out(f"\nStarting {workflow.title}: run {run_dir.name}\n")
            code = self.launch([*workflow.args, "--run-dir", str(run_dir)], child_environment(workflow.env))
        finally:
            lock.release()
        report_after_run(run_dir, code, self.out)
        return code


AUTO_CHOICE = "2"  # the automatic workflow is exactly menu option 2: scan and score only
AUTO_EXTRA_ARGS = ("--wait-for-login",)  # a login page -> wait for a manual login in the visible browser
LAUNCHER_LOG = "launcher.log"

# Opt-in automatic applications. ONLY this file selects them; no environment variable or .env key does.
WORKFLOW_FILE_NAME = "launcher_workflow.json"
SCAN_AND_SCORE_NAME, AUTO_APPLY_NAME = "scan-and-score", "auto-apply"
AUTO_APPLY_CHOICE = "auto"  # pre-flight block key for the auto-apply workflow
AUTO_APPLY_STAGES = ("scrape_jobs.py", "score_jobs.py", "tailor_job.py")
AUTO_APPLY_MAX_APPLICATIONS = 2  # APPLICATION_MAX_JOBS (.env) is used as configured, never raised
AUTO_APPLY_MIN_SCORE = 8.0
AUTO_APPLY_AGENT_KEYS = ("google_drive_folder_id",)  # tailoring needs it to produce the verified resume PDF

AUTO_APPLY_WARNING = """
AUTO-APPLY is enabled by launcher_workflow.json. This run will:
  - scan Naukri live in the visible browser with your saved profile (log in there yourself if asked)
  - export at most 2 fresh, 4+ years-eligible jobs; the AI Agent scores them and tailors a resume for each
    that scores 8+ (LLM calls; the resume PDF is created through Google Docs and uploaded to Google Drive)
  - SUBMIT applications on Naukri for at most APPLICATION_MAX_JOBS={max_jobs} of THIS RUN's jobs that pass
    every check, without asking you per job: only Naukri's own Apply button, only answers from your
    configured APPLICANT_* facts, only the verified resume made for that job, Naukri job id verified.
    Anything else (unknown question, external site, mismatch, verification page) is left to you.
Google Sheets sync and the Agent's JSON mirroring stay off. Remove launcher_workflow.json to go back to
scan-and-score only.
"""


def read_launcher_workflow(path: Path) -> str:
    """The workflow start_local.bat runs. No file: scan-and-score. Otherwise the file must be exactly
    {"workflow": "scan-and-score"} or {"workflow": "auto-apply"}; anything else raises WorkflowBlocked
    (the launcher then stops: a broken opt-in file never runs anything)."""
    path = Path(path)
    if not path.exists():
        return SCAN_AND_SCORE_NAME
    if not path.is_file():
        raise WorkflowBlocked(f"{path.name} is not a file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise WorkflowBlocked(f"{path.name} is not valid JSON ({type(e).__name__})") from None
    if not isinstance(data, dict) or set(data) != {"workflow"} \
            or data["workflow"] not in (SCAN_AND_SCORE_NAME, AUTO_APPLY_NAME):
        raise WorkflowBlocked(f'{path.name} must be exactly {{"workflow": "{SCAN_AND_SCORE_NAME}"}} or '
                              f'{{"workflow": "{AUTO_APPLY_NAME}"}}')
    return data["workflow"]


def build_auto_apply_workflow(configured_score_threshold: float, max_jobs: int) -> Workflow:
    """The auto-apply workflow. On top of SAFETY_ENV (Sheets off, artifact_sync=0, Naukri only, export
    cap 2, 24 h): AUTO mode, scrape/score/tailor, score threshold at least 8 (a stricter .env value is
    kept). APPLICATION_MAX_JOBS is not set here: the .env value is used as it is."""
    threshold = max(float(configured_score_threshold), AUTO_APPLY_MIN_SCORE)
    return Workflow(AUTO_APPLY_CHOICE, AUTO_APPLY_NAME, "Automatic applications",
                    ("--workflow", AUTO_APPLY_NAME, "--wait-for-login"),
                    {"APPLICATION_MODE": "AUTO", "AI_AGENT_STAGES": ",".join(AUTO_APPLY_STAGES),
                     "APPLICATION_SCORE_THRESHOLD": f"{threshold:g}"},
                    "", AUTO_APPLY_WARNING.format(max_jobs=max_jobs))


def auto_apply_problems(workflow: Workflow, max_jobs: int) -> list[str]:
    """Why this auto-apply workflow must not run (empty = OK). Mirrors application_pipeline.workflow_guard,
    which checks again inside the pipeline."""
    env = child_environment(workflow.env, base={})
    expected = dict(SAFETY_ENV, APPLICATION_MODE="AUTO", AI_AGENT_STAGES=",".join(AUTO_APPLY_STAGES))
    problems = [f"{key} must be {value} (is {env.get(key)})" for key, value in expected.items() if env.get(key) != value]
    try:
        if float(env.get("APPLICATION_SCORE_THRESHOLD", "")) < AUTO_APPLY_MIN_SCORE:
            problems.append(f"APPLICATION_SCORE_THRESHOLD must be at least {AUTO_APPLY_MIN_SCORE:g}")
    except ValueError:
        problems.append("APPLICATION_SCORE_THRESHOLD is not a number")
    if "--no-apply" in workflow.args or "--workflow" not in workflow.args:
        problems.append("pipeline arguments are not the auto-apply workflow")
    if not 1 <= int(max_jobs) <= AUTO_APPLY_MAX_APPLICATIONS:
        problems.append(f"APPLICATION_MAX_JOBS in .env must be 1-{AUTO_APPLY_MAX_APPLICATIONS} for auto-apply "
                        f"(is {max_jobs}); it is never changed for you")
    return problems


def agent_env_key_checks(agent_dir: Optional[Path], keys=AUTO_APPLY_AGENT_KEYS) -> list[Check]:
    """Key NAMES the AI Agent's .env must define for auto-apply (values are never read or shown)."""
    from dotenv import dotenv_values

    env_file = Path(agent_dir) / ".env" if agent_dir else None
    names = set(dotenv_values(env_file)) if env_file and env_file.is_file() else set()
    return [Check(f"Agent .env defines {key}", key in names,
                  "defined (value not shown)" if key in names else
                  f"not defined in {env_file or 'the AI Agent .env'}: tailoring cannot produce the resume PDF",
                  () if key in names else (AUTO_APPLY_CHOICE,)) for key in keys]


def run_check_config(env: dict) -> tuple[int, str]:
    """application_pipeline.py --check-config (read-only) with the enforced environment; (exit code, output)."""
    sys.stdout.flush()
    child_env = dict(env, PYTHONIOENCODING="utf-8")
    done = subprocess.run([sys.executable, str(PIPELINE_SCRIPT), "--check-config"], cwd=str(PROJECT_DIR),
                          env=child_env, capture_output=True)
    text = (done.stdout + done.stderr).decode("utf-8", "replace").replace("\r\n", "\n")
    return done.returncode, text


def write_launcher_report(run_dir: Path, started, exit_code: int, status: str, stage: str, fatal: str,
                          env: dict, error: Optional[str] = None, workflow: str = SCAN_AND_SCORE_NAME) -> None:
    """result.json for an attempt the pipeline never recorded itself (stopped before it started, or it
    ended without a report). Only facts the launcher knows; everything else is null."""
    from application_pipeline import REPORT_NAME, REPORT_SCHEMA_VERSION, _now, exit_meaning, write_json_atomic

    finished = _now()
    not_run = f"NOT RUN ({stage})"
    write_json_atomic(run_dir / REPORT_NAME, {
        "schema_version": REPORT_SCHEMA_VERSION, "run_id": run_dir.name, "status": status,
        "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "workflow": workflow, "application_mode": env.get("APPLICATION_MODE"),
        "no_apply": workflow != AUTO_APPLY_NAME,
        "options": None,
        "effective_settings": {k: env.get(k) for k in (*SAFETY_ENV, "AI_AGENT_STAGES", "APPLICATION_SCORE_THRESHOLD")},
        "exit_code": exit_code, "exit_meaning": exit_meaning(exit_code),
        "stages": None if status == "crashed" else {
            "configuration": "FAILED", "naukri_session": not_run, "scan": not_run, "export": not_run,
            "ai_agent": not_run, "applications": not_run, "google_sheets": not_run},
        "agent_export_id": None, "exported_job_ids": None, "agent_scope_report": None, "counts": None,
        "scope_validation": None, "candidates_outside_run": None, "eligibility_results": None,
        "applications": None, "application_outcomes": None,
        "warnings": [], "fatal": fatal, "error": error, "summary_text": None,
        "log_file": str(run_dir / LAUNCHER_LOG), "report_file": str(run_dir / REPORT_NAME),
        "recorded_by": "local_launcher",
    })


@dataclass
class AutoRun:
    """start_local.bat's automatic run, no menu, no prompt. Default workflow: scan and score only (menu
    option 2). workflow=build_auto_apply_workflow(...): the opt-in auto-apply workflow.

    lock -> new run folder -> read-only launcher checks -> read-only --check-config -> pipeline -> report.
    A failed check stops before Chrome opens. Nothing is retried."""
    checks: list
    runs_dir: Path
    out: Callable[[str], None] = print
    launch: Callable[..., int] = launch_pipeline
    check_config: Callable[[dict], tuple[int, str]] = run_check_config
    new_run_dir: Optional[Callable[[Path], Path]] = None
    workflow: Optional[Workflow] = None  # None: scan and score only
    source: str = f"no {WORKFLOW_FILE_NAME}: default"

    def run(self) -> int:
        from application_pipeline import REPORT_NAME, _now, load_report

        if self.workflow is None:
            try:
                workflow = build_workflow(AUTO_CHOICE, [])  # refuses anything that could tailor or submit
            except WorkflowBlocked as e:
                self.out(f"STOPPED: {e}. Nothing was run.")
                return EXIT_CONFIG
            args = [*workflow.args, *AUTO_EXTRA_ARGS]
            self.out("NAUKRI AUTO JOB SCAN - scan and score only (APPLICATION_MODE=SAFE, --no-apply).")
            self.out("No application is submitted. No resume is tailored. Nothing is uploaded to Google Drive "
                     "or written to Google Sheets.")
        else:
            workflow = self.workflow
            args = list(workflow.args)
            self.out(f"NAUKRI AUTO-APPLY - automatic applications ({self.source}).")
            self.out(workflow.warning)
        env = child_environment(workflow.env)
        lock = LauncherLock(self.runs_dir / LOCK_NAME)
        if not lock.acquire():
            self.out("Another launcher run is in progress (output\\runs\\.launcher.lock is held); nothing was run.")
            return EXIT_CONFIG
        try:
            if self.new_run_dir is None:
                from application_pipeline import new_run_dir
                run_dir = new_run_dir(self.runs_dir)
            else:
                run_dir = self.new_run_dir(self.runs_dir)
            started = _now()
            with open(run_dir / LAUNCHER_LOG, "x", encoding="utf-8") as log:
                def say(text=""):
                    self.out(text)
                    log.write(text + "\n")
                    log.flush()

                say(f"Run {run_dir.name} started {started.isoformat()}")
                say(f"Workflow: {workflow.workflow} ({self.source})")
                say("Enforced settings: " + ", ".join(
                    f"{k}={v}" for k, v in sorted(env.items())
                    if k in SAFETY_ENV or k in ("AI_AGENT_STAGES", "APPLICATION_SCORE_THRESHOLD"))
                    + " | pipeline: " + " ".join(args))
                for check in self.checks:
                    mark = "OK  " if check.ok else ("FAIL" if check.blocks else "WARN")
                    say(f"  [{mark}] {check.name:<34} {check.detail}")
                reasons = blocked_choices(self.checks).get(workflow.choice)
                if reasons:
                    fatal = "pre-flight checks failed: " + "; ".join(reasons)
                    say(f"\nSTOPPED before the scan: {fatal}")
                    write_launcher_report(run_dir, started, EXIT_CONFIG, "failed", "pre-flight failed", fatal,
                                          env, workflow=workflow.workflow)
                    return self._finish(run_dir, EXIT_CONFIG, say)
                say("\nConfiguration check (read-only):")
                code, text = self.check_config(env)
                for line in text.rstrip("\n").splitlines():
                    say(line)
                if code != EXIT_OK:
                    fatal = f"configuration check failed (exit code {code}); see {LAUNCHER_LOG}"
                    say(f"\nSTOPPED before the scan: {fatal}")
                    write_launcher_report(run_dir, started, EXIT_CONFIG, "failed", "configuration check failed",
                                          fatal, env, workflow=workflow.workflow)
                    return self._finish(run_dir, EXIT_CONFIG, say)
                say(f"\nStarting the {workflow.workflow} run (pipeline log: {run_dir / 'run.log'})\n")
            code = self.launch([*args, "--run-dir", str(run_dir)], env)
            report, _ = load_report(run_dir / REPORT_NAME)
            if report is None:
                write_launcher_report(run_dir, started, code if code != EXIT_OK else EXIT_CONFIG, "crashed",
                                      "pipeline ended without a report", "the pipeline wrote no result.json",
                                      env, error=f"pipeline exit code {code}, no report", workflow=workflow.workflow)
                if code == EXIT_OK:  # a run without its report is never reported as complete
                    code = EXIT_CONFIG
        finally:
            lock.release()
        report_after_run(run_dir, code, self.out)
        return code

    def _finish(self, run_dir: Path, code: int, say) -> int:
        say(f"Exit code: {code}. Report: {run_dir / 'result.json'}  Log: {run_dir / LAUNCHER_LOG}")
        say("RUN DID NOT COMPLETE SUCCESSFULLY")
        return code


def main(argv: Optional[list] = None) -> int:
    """Default (start_local.bat): the automatic scan-and-score run. --menu: the interactive developer menu.
    Any other argument is refused, so nothing can weaken the enforced settings from the command line."""
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--menu"]):
        print("local_launcher.py takes no arguments (or --menu for the developer menu); nothing was run.")
        return EXIT_CONFIG
    selected = SCAN_AND_SCORE_NAME
    if argv == []:  # start_local.bat: the workflow comes ONLY from launcher_workflow.json
        try:
            selected = read_launcher_workflow(PROJECT_DIR / WORKFLOW_FILE_NAME)
        except WorkflowBlocked as e:
            print(f"STOPPED: {e}. Nothing was run. Fix or delete {WORKFLOW_FILE_NAME} "
                  "(no file = scan and score only).")
            return EXIT_CONFIG
    os.environ.update(SAFETY_ENV)  # before config is imported: this process sees the enforced values too
    if argv == []:
        os.environ["AI_AGENT_STAGES"] = ",".join(AUTO_APPLY_STAGES if selected == AUTO_APPLY_NAME
                                                 else SCAN_AND_SCORE_STAGES)
        if selected == AUTO_APPLY_NAME:
            os.environ["APPLICATION_MODE"] = "AUTO"
    import config
    from application_pipeline import RUNS_DIR

    if argv == ["--menu"]:
        checks = preflight_checks(config.AI_AGENT_DIR, config.AI_AGENT_OUTPUT_DIR, config.AI_AGENT_PYTHON,
                                  list(config.AI_AGENT_STAGES))
        blocked = blocked_choices(checks)
        if blocked:
            print_checks(checks, print)
            print("\nOptions " + ", ".join(sorted(blocked)) + " are blocked until the FAIL lines above are fixed.")
        return Launcher(checks, list(config.AI_AGENT_STAGES), RUNS_DIR).run()
    if selected == AUTO_APPLY_NAME:
        workflow = build_auto_apply_workflow(config.APPLICATION_SCORE_THRESHOLD, config.APPLICATION_MAX_JOBS)
        os.environ["APPLICATION_SCORE_THRESHOLD"] = workflow.env["APPLICATION_SCORE_THRESHOLD"]
        checks = preflight_checks(config.AI_AGENT_DIR, config.AI_AGENT_OUTPUT_DIR, config.AI_AGENT_PYTHON,
                                  list(AUTO_APPLY_STAGES))
        checks += agent_env_key_checks(config.AI_AGENT_DIR)
        problems = auto_apply_problems(workflow, config.APPLICATION_MAX_JOBS)
        checks.append(Check("Auto-apply settings", not problems, "; ".join(problems) or
                            f"AUTO, APPLICATION_MAX_JOBS={config.APPLICATION_MAX_JOBS}, score >= "
                            f"{workflow.env['APPLICATION_SCORE_THRESHOLD']}, export cap 2, 24 h, Sheets off",
                            (AUTO_APPLY_CHOICE,) if problems else ()))
        return AutoRun(checks, RUNS_DIR, workflow=workflow, source=f"selected by {WORKFLOW_FILE_NAME}").run()
    # Option 2's stages are fixed, so the configured AI_AGENT_STAGES (option 3 only) is not checked here.
    checks = preflight_checks(config.AI_AGENT_DIR, config.AI_AGENT_OUTPUT_DIR, config.AI_AGENT_PYTHON,
                              list(SCAN_AND_SCORE_STAGES))
    return AutoRun(checks, RUNS_DIR).run()


if __name__ == "__main__":
    sys.exit(main())
