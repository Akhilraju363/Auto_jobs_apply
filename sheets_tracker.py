"""Phase 4 Google Sheets tracking of Naukri application outcomes.

Writes to the AI Agent's existing "Job Application Tracker" sheet with the same contract as its
scripts/write_sheet.py: the `gws` CLI (whose own stored login is reused -- this project holds no
Google credentials), the same sheet id (google_sheet_id), the same Sheet1!A:L columns and the same
dedup rule (one row per canonical job link). Naukri-only facts go in extra columns M:S that the
Agent's reader (A:L) ignores, so the Agent and its dashboard keep working unchanged.

The Sheet is reporting only. SQLite (db.py) is the source of truth for duplicate prevention: an
outcome is queued in db.sheet_sync first, and a Sheets failure leaves that row PENDING for the next
run. Nothing here can change application history or trigger an application.
"""

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import dotenv_values

from application_outcome import OUTCOME_APPLIED, application_outcome_label
from db import (
    canonical_job_url,
    find_successful_application,
    get_application_record,
    get_job,
    get_sheet_syncs,
    mark_sheet_sync_failed,
    mark_sheet_synced,
    queue_sheet_sync,
)

logger = logging.getLogger(__name__)

# Columns A:L exactly as the AI Agent's write_sheet.HEADERS (do not reorder).
AGENT_HEADERS = ["Job Title", "Company", "Job Link", "Fit Score", "Resume Path", "Status", "Timestamp",
                 "Company Notes", "Source", "Status Updated", "Resume ID", "Match %"]
# Columns M:S, Naukri application facts.
NAUKRI_HEADERS = ["Application State", "Applied At", "Naukri Job ID", "Location", "Experience",
                  "AI Recommendation", "Failure Reason"]
HEADERS = AGENT_HEADERS + NAUKRI_HEADERS
LAST_COLUMN = "S"
# The Agent's sheet has a strict dropdown on Status (F): only these values are accepted.
STATUS_OPTIONS = ["Not Applied", "Applied", "Interviewing", "Offer", "Rejected"]
SOURCE = "Naukri"
_COL = {name: index for index, name in enumerate(HEADERS)}

# Sync result codes (also stored in sheet_sync.last_error_code).
SYNCED = "SYNCED"
SYNC_PENDING = "SYNC_PENDING"
AUTH_REQUIRED = "AUTH_REQUIRED"
GWS_NOT_INSTALLED = "GWS_NOT_INSTALLED"
SHEET_NOT_CONFIGURED = "SHEET_NOT_CONFIGURED"
SYNC_FAILED = "SYNC_FAILED"
DISABLED = "DISABLED"
# Failures that affect every row: stop the pass instead of failing each job separately.
_GLOBAL_FAILURES = (AUTH_REQUIRED, GWS_NOT_INSTALLED, SHEET_NOT_CONFIGURED)

_AUTH_MARKERS = ("invalid_grant", "unauthenticated", "unauthorized", "401", "not authenticated",
                 "no credentials", "credentials not found", "gws auth login", "token has been expired",
                 "token expired", "login required", "refresh token")


class SheetsError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def classify_gws_error(text: str) -> str:
    lowered = str(text or "").lower()
    return AUTH_REQUIRED if any(marker in lowered for marker in _AUTH_MARKERS) else SYNC_FAILED


def resolve_gws_command() -> list[str]:
    """Same resolution as the AI Agent's write_sheet._resolve_gws(): on Windows call node with the
    package's run.js directly, bypassing the .cmd shim."""
    if os.name != "nt":
        return ["gws"]
    gws_cmd = shutil.which("gws.cmd")
    if gws_cmd:
        run_js = os.path.join(os.path.dirname(gws_cmd), "node_modules", "@googleworkspace", "cli", "run.js")
        if os.path.exists(run_js):
            return ["node", run_js]
    return ["gws"]


class GwsClient:
    """One `gws` call per invocation; failures become SheetsError with a code. Never logs values."""

    def __init__(self, command: Optional[list[str]] = None, runner: Callable[..., Any] = subprocess.run,
                 timeout_seconds: int = 60):
        self.command = command or resolve_gws_command()
        self.runner, self.timeout = runner, timeout_seconds

    def __call__(self, *args: str) -> dict:
        try:
            result = self.runner([*self.command, *args], capture_output=True, text=True, encoding="utf-8",
                                 timeout=self.timeout)
        except FileNotFoundError:
            raise SheetsError(GWS_NOT_INSTALLED, "the gws CLI is not installed (see the AI Agent's GWS_SETUP.md)")
        except subprocess.TimeoutExpired:
            raise SheetsError(SYNC_FAILED, f"gws did not answer within {self.timeout}s")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            message = detail[-1][:200] if detail else f"exit code {result.returncode}"
            raise SheetsError(classify_gws_error(result.stderr or result.stdout), f"gws {args[0]} failed: {message}")
        try:
            return json.loads(result.stdout) if (result.stdout or "").strip() else {}
        except json.JSONDecodeError:
            raise SheetsError(SYNC_FAILED, "gws returned unreadable output")


def resolve_sheet_id(configured: Optional[str], agent_dir: Optional[Path]) -> Optional[str]:
    """GOOGLE_SHEET_ID, else the AI Agent's google_sheet_id (only that key is read from its .env)."""
    if configured and configured.strip():
        return configured.strip()
    env_file = Path(agent_dir) / ".env" if agent_dir else None
    if env_file and env_file.exists():
        value = (dotenv_values(env_file).get("google_sheet_id") or "").strip()
        return value or None
    return None


def _cell(value: Any) -> str:
    """Plain text cell. The Agent writes with USER_ENTERED, so text that looks like a formula is
    quoted to stay text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def tracker_status(outcome: str, current: str = "") -> str:
    """Status (F). Only a confirmed application sets Applied; a status the user moved on
    (Interviewing, Offer, Rejected) is never overwritten."""
    current = (current or "").strip()
    if current and current not in ("Not Applied", "Applied"):
        return current
    if outcome == OUTCOME_APPLIED or current == "Applied":
        return "Applied"
    return "Not Applied"


def build_row(job: dict, record: Optional[dict], now: datetime, current_status: str = "") -> list[str]:
    """One A:S tracker row for a Naukri job and its current application outcome."""
    outcome = application_outcome_label(job.get("application_state"), job.get("application_code"))
    applied = outcome == OUTCOME_APPLIED
    score = job.get("match_score")
    today = now.date().isoformat()
    values = {
        "Job Title": job.get("title"),
        "Company": job.get("company"),
        "Job Link": job.get("url"),
        "Fit Score": "" if score is None else int(round(float(score) / 10)),
        "Resume Path": (record or {}).get("resume_path") or "",
        "Status": tracker_status(outcome, current_status),
        "Timestamp": today,
        "Source": SOURCE,
        "Status Updated": today,
        "Application State": outcome,
        "Applied At": (record or {}).get("applied_at") if applied else "",
        "Naukri Job ID": job.get("job_id") or "",
        "Location": job.get("location") or "",
        "Experience": job.get("experience_text") or "",
        "AI Recommendation": job.get("match_status") or "",
        "Failure Reason": "" if applied else (job.get("application_reason") or ""),
    }
    return [_cell(values.get(name, "")) for name in HEADERS]


class SheetTracker:
    def __init__(self, gws: Callable[..., dict], sheet_id: str, tab: str = "Sheet1",
                 clock: Callable[[], datetime] = datetime.now):
        self.gws, self.sheet_id, self.tab, self.clock = gws, sheet_id, tab, clock
        self._rows: Optional[list[list[str]]] = None
        self._headers_checked = False

    def _update(self, a1_range: str, values: list[list[str]]) -> None:
        self.gws("sheets", "spreadsheets", "values", "update", "--params",
                 json.dumps({"spreadsheetId": self.sheet_id, "range": f"{self.tab}!{a1_range}",
                             "valueInputOption": "USER_ENTERED"}),
                 "--json", json.dumps({"values": values}))

    def rows(self) -> list[list[str]]:
        if self._rows is None:
            result = self.gws("sheets", "spreadsheets", "values", "get", "--params",
                              json.dumps({"spreadsheetId": self.sheet_id, "range": f"{self.tab}!A:{LAST_COLUMN}"}))
            self._rows = [[str(c) for c in row] for row in result.get("values", [])]
        return self._rows

    def ensure_headers(self) -> None:
        """Add the Naukri headers (M:S) once; A:L belong to the AI Agent and are only written on an
        empty sheet."""
        if self._headers_checked:
            return
        rows = self.rows()
        if not rows:
            self._update(f"A1:{LAST_COLUMN}1", [HEADERS])
            rows.append(list(HEADERS))
        elif rows[0][len(AGENT_HEADERS):len(HEADERS)] != NAUKRI_HEADERS:
            self._update(f"M1:{LAST_COLUMN}1", [NAUKRI_HEADERS])
            rows[0] = (rows[0] + [""] * len(AGENT_HEADERS))[:len(AGENT_HEADERS)] + list(NAUKRI_HEADERS)
        self._headers_checked = True

    def find_row(self, job: dict) -> Optional[int]:
        """1-based sheet row for this job: canonical link first, then the Naukri job id. Never company."""
        url = canonical_job_url(job.get("url"))
        job_id = str(job.get("job_id") or "").strip()
        rows = self.rows()
        for number, row in enumerate(rows[1:], start=2):
            cells = row + [""] * (len(HEADERS) - len(row))
            if url and canonical_job_url(cells[_COL["Job Link"]]) == url:
                return number
        for number, row in enumerate(rows[1:], start=2):
            cells = row + [""] * (len(HEADERS) - len(row))
            if job_id and cells[_COL["Naukri Job ID"]].lstrip("'") == job_id:
                return number
        return None

    def upsert(self, job: dict, record: Optional[dict]) -> int:
        """Write the job's outcome: update its existing row (e.g. the one the Agent added when it tailored
        the resume) or append one. Returns the sheet row number."""
        self.ensure_headers()
        rows, number = self.rows(), self.find_row(job)
        if number is None:
            row = build_row(job, record, self.clock())
            self.gws("sheets", "spreadsheets", "values", "append", "--params",
                     json.dumps({"spreadsheetId": self.sheet_id, "range": f"{self.tab}!A:{LAST_COLUMN}",
                                 "valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}),
                     "--json", json.dumps({"values": [row]}))
            rows.append(row)
            return len(rows)
        existing = rows[number - 1] + [""] * (len(HEADERS) - len(rows[number - 1]))
        row = build_row(job, record, self.clock(), current_status=existing[_COL["Status"]])
        self._update(f"F{number}", [[row[_COL["Status"]]]])
        self._update(f"J{number}", [[row[_COL["Status Updated"]]]])
        if not existing[_COL["Resume Path"]] and row[_COL["Resume Path"]]:
            self._update(f"E{number}", [[row[_COL["Resume Path"]]]])
        self._update(f"M{number}:{LAST_COLUMN}{number}", [row[len(AGENT_HEADERS):]])
        rows[number - 1] = existing[:len(AGENT_HEADERS)] + row[len(AGENT_HEADERS):]
        rows[number - 1][_COL["Status"]] = row[_COL["Status"]]
        return number


@dataclass
class SyncReport:
    status: str = SYNCED  # SYNCED, SYNC_PENDING, AUTH_REQUIRED, GWS_NOT_INSTALLED, SHEET_NOT_CONFIGURED, DISABLED
    synced: int = 0
    pending: int = 0
    errors: list[str] = field(default_factory=list)


def sync_pending_outcomes(tracker: Optional[SheetTracker], unavailable_code: Optional[str] = None,
                          unavailable_reason: str = "") -> SyncReport:
    """Push every PENDING sheet_sync row to the tracker. On any failure the row stays PENDING
    (retried next run); application history is never touched."""
    report = SyncReport()
    pending = get_sheet_syncs()
    if tracker is None:
        report.status = unavailable_code or SHEET_NOT_CONFIGURED
        if report.status != DISABLED:
            for row in pending:
                mark_sheet_sync_failed(row["job_row_id"], report.status, unavailable_reason)
        report.pending = len(pending)
        if unavailable_reason:
            report.errors.append(unavailable_reason)
        return report
    stop: Optional[SheetsError] = None
    for row in pending:
        job_row_id = row["job_row_id"]
        if stop is not None:
            mark_sheet_sync_failed(job_row_id, stop.code, str(stop))
            continue
        job = get_job(job_row_id)
        if job is None:
            mark_sheet_sync_failed(job_row_id, SYNC_FAILED, "job no longer in the database")
            report.errors.append(f"job {job_row_id}: no longer in the database")
            continue
        queue_sheet_sync(job_row_id)  # report the job's current outcome, even if it moved on since queueing
        record = find_successful_application(job) or get_application_record(job)
        try:
            tracker.upsert(job, record)
        except SheetsError as e:
            mark_sheet_sync_failed(job_row_id, e.code, str(e))
            report.errors.append(f"job {job_row_id}: {e.code}: {e}")
            logger.warning(f"Google Sheets sync failed for job {job_row_id}: {e.code}")
            if e.code in _GLOBAL_FAILURES:
                stop = e
            continue
        if mark_sheet_synced(job_row_id, job.get("application_state"), job.get("application_code")):
            report.synced += 1
            logger.info(f"Google Sheets synced | db_id={job_row_id} | "
                        f"{application_outcome_label(job.get('application_state'), job.get('application_code'))}")
    report.pending = len(get_sheet_syncs())
    if stop is not None:
        report.status = stop.code
    elif report.pending:
        report.status = SYNC_PENDING
    return report


__all__ = [
    "AGENT_HEADERS",
    "AUTH_REQUIRED",
    "DISABLED",
    "GWS_NOT_INSTALLED",
    "GwsClient",
    "HEADERS",
    "NAUKRI_HEADERS",
    "SHEET_NOT_CONFIGURED",
    "SYNCED",
    "SYNC_FAILED",
    "SYNC_PENDING",
    "SheetTracker",
    "SheetsError",
    "SyncReport",
    "build_row",
    "classify_gws_error",
    "resolve_gws_command",
    "resolve_sheet_id",
    "sync_pending_outcomes",
    "tracker_status",
]
