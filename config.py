import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent


def resolve_project_path(value: str) -> Path:
    """Absolute paths are kept; relative paths are resolved against the project directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


# Single resume source for matching (plain text). Override with RESUME_PATH in .env.
RESUME_PATH = resolve_project_path(os.getenv("RESUME_PATH", "").strip() or "resume.txt")
# Phase 4: the one local SQLite database (operational source of truth for application history).
# jobs.db is the pre-Phase-4 location; db.init_db() copies it here once and leaves it untouched.
DEFAULT_DB_PATH = BASE_DIR / "data" / "naukri_auto_apply.db"
LEGACY_DB_PATH = BASE_DIR / "jobs.db"
DB_PATH = resolve_project_path(os.getenv("NAUKRI_DB_PATH", "").strip() or str(DEFAULT_DB_PATH))
CHROME_USER_DATA = BASE_DIR / "chrome_user_data"

MATCH_THRESHOLD = 10.0
DAILY_LIMIT = 25

JOB_TITLES = [
    "Software Developer",
    "Software Development Engineer",
    "SDE",
    "Backend Engineer",
    "Full Stack Developer",
]

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    return default if not value else value in ("1", "true", "yes", "on")


def _env_number(name: str, default, cast=int):
    value = os.getenv(name, "").strip()
    try:
        return cast(value) if value else default
    except ValueError:
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    items = [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]
    return items or list(default)


# Naukri Phase 1 job discovery (override any of these in .env)
DEFAULT_NAUKRI_KEYWORDS = [
    "Java Full Stack Developer",
    "Java Developer",
    "Full Stack Developer",
    "Software Engineer",
    "Java Spring Boot Developer",
    "Java Backend Developer",
]

NAUKRI_ENABLED = _env_bool("NAUKRI_ENABLED", True)
NAUKRI_KEYWORDS = _env_list("NAUKRI_KEYWORDS", DEFAULT_NAUKRI_KEYWORDS)
NAUKRI_LOCATION = os.getenv("NAUKRI_LOCATION", "").strip()
NAUKRI_EXPERIENCE = _env_number("NAUKRI_EXPERIENCE", None)
NAUKRI_MAX_PAGES = max(1, _env_number("NAUKRI_MAX_PAGES", 5))
NAUKRI_MAX_JOBS = max(1, _env_number("NAUKRI_MAX_JOBS", 50))
APPLICATION_JOB_WINDOW_HOURS = max(1, _env_number("APPLICATION_JOB_WINDOW_HOURS", 24))
APPLICATION_SCORE_THRESHOLD = _env_number("APPLICATION_SCORE_THRESHOLD", 8, float)
PREFERRED_LOCATIONS = _env_list("PREFERRED_LOCATIONS", [])
NAUKRI_DELAY_MIN, NAUKRI_DELAY_MAX = sorted(
    (
        max(0.0, _env_number("NAUKRI_DELAY_MIN", 2.0, float)),
        max(0.0, _env_number("NAUKRI_DELAY_MAX", 5.0, float)),
    )
)

# AI Agent file handoff (Phase 2). The AI Agent is a separate repository; only file paths are shared.
# AI_AGENT_OUTPUT_DIR: the AI Agent's output/ folder (scored_jobs.json is read from it, never written).
_ai_agent_output = os.getenv("AI_AGENT_OUTPUT_DIR", "").strip()
AI_AGENT_OUTPUT_DIR = resolve_project_path(_ai_agent_output) if _ai_agent_output else None
NAUKRI_EXPORT_PATH = resolve_project_path(
    os.getenv("NAUKRI_EXPORT_PATH", "").strip() or "exports/naukri_jobs.json"
)
AI_AGENT_EXPORT_LIMIT = max(1, _env_number("AI_AGENT_EXPORT_LIMIT", 2))

# Phase 3 application preparation (headed browser only; Naukri login comes from the saved
# chrome_user_data/ profile, never from stored credentials).
NAUKRI_APPLICATION_TIMEOUT = max(5, _env_number("NAUKRI_APPLICATION_TIMEOUT", 30))  # seconds per page wait
APPLICATION_MAX_JOBS = max(1, _env_number("APPLICATION_MAX_JOBS", 1))  # jobs per `--action apply` run
NAUKRI_LOGIN_WAIT_MINUTES = max(1, _env_number("NAUKRI_LOGIN_WAIT_MINUTES", 10))

# Applicant facts for application questions are read by application_profile.ApplicationProfile
# (APPLICANT_* in .env). The master resume belongs to the AI Agent; it is read (never written) only
# to confirm "do you have experience with X?" questions.
_master_resume = os.getenv("MASTER_RESUME_PATH", "").strip()
MASTER_RESUME_PATH = (
    resolve_project_path(_master_resume)
    if _master_resume
    else (AI_AGENT_OUTPUT_DIR.parent / "resume" / "base_resume.md" if AI_AGENT_OUTPUT_DIR else None)
)

# Phase 3.3 recovery and outcome tracking.
NAUKRI_NAVIGATION_RETRIES = min(5, max(0, _env_number("NAUKRI_NAVIGATION_RETRIES", 2)))  # page loads before Apply only
CONFIRMATION_WAIT_SECONDS = max(0, _env_number("CONFIRMATION_WAIT_SECONDS", 20))  # extra wait for a delayed confirmation
APPLICATION_DEBUG = _env_bool("APPLICATION_DEBUG", True)  # sanitized failure diagnostics (no page text, no answers)
APPLICATION_DEBUG_DIR = resolve_project_path(os.getenv("APPLICATION_DEBUG_DIR", "").strip() or "output/application_debug")

# Phase 4 local orchestrator (application_pipeline.py / start_local.bat).
# APPLICATION_MODE: SAFE (default; every submission needs your 'y') or AUTO. Read raw here and
# validated by application_pipeline.validate_configuration(): an unknown value is an error, never AUTO.
APPLICATION_MODE = (os.getenv("APPLICATION_MODE", "").strip() or "SAFE").upper()
# The AI Agent checkout. Default: the folder that contains AI_AGENT_OUTPUT_DIR.
_ai_agent_dir = os.getenv("AI_AGENT_DIR", "").strip()
AI_AGENT_DIR = (
    resolve_project_path(_ai_agent_dir) if _ai_agent_dir else (AI_AGENT_OUTPUT_DIR.parent if AI_AGENT_OUTPUT_DIR else None)
)
# The AI Agent's own interpreter. Default: <AI_AGENT_DIR>/.venv/Scripts/python.exe (or bin/python).
_ai_agent_python = os.getenv("AI_AGENT_PYTHON", "").strip()
AI_AGENT_PYTHON = resolve_project_path(_ai_agent_python) if _ai_agent_python else None
# The AI Agent stage scripts the pipeline runs, in order (scripts/<name> inside AI_AGENT_DIR).
AI_AGENT_STAGES = _env_list("AI_AGENT_STAGES", ["scrape_jobs.py", "score_jobs.py", "tailor_job.py"])
AI_AGENT_TIMEOUT_MINUTES = max(1, _env_number("AI_AGENT_TIMEOUT_MINUTES", 60))
# Google Sheets tracking through the AI Agent's tracker sheet and its `gws` CLI login.
GOOGLE_SHEETS_ENABLED = _env_bool("GOOGLE_SHEETS_ENABLED", True)
# Empty: reuse google_sheet_id from the AI Agent's .env (only that key is read).
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SHEET_TAB = os.getenv("GOOGLE_SHEET_TAB", "").strip() or "Sheet1"

__all__ = [
    "BASE_DIR",
    "RESUME_PATH",
    "DEFAULT_DB_PATH",
    "LEGACY_DB_PATH",
    "DB_PATH",
    "CHROME_USER_DATA",
    "MATCH_THRESHOLD",
    "DAILY_LIMIT",
    "JOB_TITLES",
    "GEMINI_API_KEY",
    "NAUKRI_ENABLED",
    "NAUKRI_KEYWORDS",
    "NAUKRI_LOCATION",
    "NAUKRI_EXPERIENCE",
    "NAUKRI_MAX_PAGES",
    "NAUKRI_MAX_JOBS",
    "APPLICATION_JOB_WINDOW_HOURS",
    "APPLICATION_SCORE_THRESHOLD",
    "PREFERRED_LOCATIONS",
    "NAUKRI_DELAY_MIN",
    "NAUKRI_DELAY_MAX",
    "AI_AGENT_OUTPUT_DIR",
    "NAUKRI_EXPORT_PATH",
    "AI_AGENT_EXPORT_LIMIT",
    "NAUKRI_APPLICATION_TIMEOUT",
    "APPLICATION_MAX_JOBS",
    "NAUKRI_LOGIN_WAIT_MINUTES",
    "MASTER_RESUME_PATH",
    "NAUKRI_NAVIGATION_RETRIES",
    "CONFIRMATION_WAIT_SECONDS",
    "APPLICATION_DEBUG",
    "APPLICATION_DEBUG_DIR",
    "APPLICATION_MODE",
    "AI_AGENT_DIR",
    "AI_AGENT_PYTHON",
    "AI_AGENT_STAGES",
    "AI_AGENT_TIMEOUT_MINUTES",
    "GOOGLE_SHEETS_ENABLED",
    "GOOGLE_SHEET_ID",
    "GOOGLE_SHEET_TAB",
]
