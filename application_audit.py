"""Application event logging and sanitized failure diagnostics (Phase 3.3).

Events: one structured log line per transition (APPLICATION_START ... RECOVERY_REQUIRED) with the
job id, canonical URL, event, state and timestamp. Diagnostics: a small JSON file per failure under
APPLICATION_DEBUG_DIR/<job id>/<timestamp>/state.json with URL (no query), title, detected buttons,
structure counts, state/code/reason and resume metadata.

Never written or logged: passwords, OTPs, CAPTCHA content, cookies, tokens, headers, page body
text, answers. Emails, phone numbers and configured applicant values are redacted; screenshots are
not taken (they would capture personal data).
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit, urlunsplit

from config import APPLICATION_DEBUG, APPLICATION_DEBUG_DIR

events_log = logging.getLogger("application_events")

EVENTS = (
    "APPLICATION_START", "AUTH_CHECK", "JOB_OPENED", "JOB_STATE_CHECKED", "RESUME_RESOLVED", "RESUME_UPLOADED",
    "QUESTION_DETECTED", "QUESTION_AUTO_ANSWERED", "QUESTION_MANUAL_REQUIRED", "REVIEW_SHOWN", "APPROVAL_RECEIVED",
    "APPLY_CLICKED", "SUBMISSION_PENDING", "CONFIRMATION_DETECTED", "APPLICATION_CONFIRMED", "APPLICATION_FAILED",
    "RECOVERY_REQUIRED", "RECOVERY_START", "RECOVERY_RESULT", "IDEMPOTENCY_BLOCKED",
)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)")
_MONEY = re.compile(r"\b\d+(?:\.\d+)?\s*(?:lpa|lakhs?|lacs?|ctc)\b", re.I)
REDACTED = "[REDACTED]"


def strip_query(url: Optional[str]) -> str:
    if not url:
        return ""
    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


_NOT_SECRET = ("true", "false", "yes", "no", "y", "n", "0", "1")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask emails, phone numbers, salary-like amounts and any configured applicant value."""
    out = redact_secrets(text, secrets)
    out = _EMAIL.sub(REDACTED, out)
    out = _PHONE.sub(REDACTED, out)
    return _MONEY.sub(REDACTED, out)


def log_event(event: str, job: dict, state: Optional[str] = None, secrets: Iterable[str] = (), **fields) -> None:
    """One structured line; values pass through redact(). Never pass answers or page text."""
    if event not in EVENTS:
        raise ValueError(f"unknown application event {event}")
    extra = " | ".join(f"{k}={redact(v, secrets)}" for k, v in fields.items() if v not in (None, ""))
    events_log.info(f"{event} | job_id={job.get('id')} | naukri_id={job.get('job_id')} | url={strip_query(job.get('url'))} | "
                    f"state={state or job.get('application_state') or '-'} | ts={datetime.now().isoformat(timespec='seconds')}"
                    + (f" | {extra}" if extra else ""))


def build_diagnostics(job: dict, state: str, code: Optional[str], reason: str, snapshot: Optional[dict] = None,
                      structure: Optional[dict] = None, resume: Optional[dict] = None, secrets: Iterable[str] = ()) -> dict:
    """The sanitized diagnostic record (pure, testable)."""
    snapshot = snapshot or {}
    secrets = list(secrets)
    data = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "job": {"id": job.get("id"), "naukri_id": job.get("job_id"), "title": job.get("title"),
                "company": job.get("company"), "url": strip_query(job.get("url"))},
        "state": state, "code": code, "reason": reason,
        "page": {
            "url": strip_query(snapshot.get("url")), "title": snapshot.get("title"),
            "login_links": snapshot.get("login_links"), "logged_in_markers": snapshot.get("logged_in_markers"),
            "has_job_header": snapshot.get("has_job_header"), "has_captcha_frame": snapshot.get("has_captcha_frame"),
            "buttons": [{k: b.get(k) for k in ("id", "text", "disabled", "in_job_header") if k in b}
                        for b in snapshot.get("buttons") or [] if isinstance(b, dict)],
            "header_controls": list(snapshot.get("header_controls") or []),
        },
        "structure": structure or {},
        "resume": resume or {},
    }
    return _redact_values(data, secrets)


# Public Naukri identifiers (job id, query-less job URL): only configured values and emails are masked
# there -- the phone pattern would otherwise eat the 12-digit job id.
_IDENTIFIER_KEYS = ("url", "naukri_id")


def _redact_values(value, secrets: list, key: str = ""):
    """Redact every string inside a structure (never the JSON syntax itself)."""
    if isinstance(value, dict):
        return {k: _redact_values(v, secrets, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_values(v, secrets, key) for v in value]
    if not isinstance(value, str):
        return value
    if key in _IDENTIFIER_KEYS:
        return _EMAIL.sub(REDACTED, redact_secrets(value, secrets))
    return redact(value, secrets)


def redact_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    out = str(text)
    secrets = {str(s).strip() for s in secrets if s and len(str(s).strip()) >= 2 and str(s).strip().lower() not in _NOT_SECRET}
    for secret in sorted(secrets, key=len, reverse=True):
        out = re.sub(rf"(?<![\w.]){re.escape(secret)}(?![\w])", REDACTED, out, flags=re.I)
    return out


def write_diagnostics(job: dict, record: dict, base_dir: Optional[Path] = None, enabled: Optional[bool] = None) -> Optional[Path]:
    """Write state.json for a failure; returns its path (None when disabled)."""
    if not (APPLICATION_DEBUG if enabled is None else enabled):
        return None
    folder = Path(base_dir or APPLICATION_DEBUG_DIR) / str(job.get("id")) / datetime.now().strftime("%Y%m%dT%H%M%S%f")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "state.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


__all__ = ["EVENTS", "REDACTED", "build_diagnostics", "log_event", "redact", "strip_query", "write_diagnostics"]
