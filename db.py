import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from config import DB_PATH

# Discovered jobs (Phase 1). Kept separate from applied_jobs so discovery never
# counts toward the daily application limit or marks a job as applied.
# Columns are added with ALTER TABLE when missing, so existing databases migrate
# in place without losing data.
JOB_COLUMNS = {
    "job_id": "TEXT",
    "title": "TEXT NOT NULL DEFAULT ''",
    "company": "TEXT NOT NULL DEFAULT ''",
    "location": "TEXT",
    "experience_min": "INTEGER",
    "experience_max": "INTEGER",
    "experience_text": "TEXT",
    "salary_min": "INTEGER",
    "salary_max": "INTEGER",
    "salary_text": "TEXT",
    "employment_type": "TEXT",
    "job_description": "TEXT",
    "skills": "TEXT",
    "posted_date": "TEXT",
    "posted_label": "TEXT",
    "url": "TEXT NOT NULL DEFAULT ''",
    "apply_url": "TEXT",
    "is_easy_apply": "INTEGER NOT NULL DEFAULT 0",
    "is_already_applied": "INTEGER NOT NULL DEFAULT 0",
    "has_apply_button": "INTEGER NOT NULL DEFAULT 0",
    "search_keyword": "TEXT",
    "metadata": "TEXT",
    "scraped_at": "TIMESTAMP",
    "posted_at": "TIMESTAMP",
    "updated_at": "TIMESTAMP",
    "freshness_status": "TEXT",
    "freshness_checked_at": "TIMESTAMP",
}

# Job lifecycle: discovered (Phase 1) -> scored (Phase 2) with match_status
# recommended / review / skipped. Application states belong to a later phase.
JOB_STATUS_DISCOVERED = "discovered"
JOB_STATUS_SCORED = "scored"
MATCH_STATUSES = ("recommended", "review", "skipped")

# Written only by save_job_score(); NULL until a job is scored.
SCORING_COLUMNS = {
    "match_score": "REAL",  # 0-100
    "match_status": "TEXT",  # one of MATCH_STATUSES
    "match_reason": "TEXT",
    "matching_skills": "TEXT",  # JSON list
    "missing_skills": "TEXT",  # JSON list
    "experience_match": "INTEGER",  # nullable boolean
    "location_match": "INTEGER",  # nullable boolean
    "scored_at": "TIMESTAMP",
}
_JSON_LIST_COLUMNS = ("skills", "matching_skills", "missing_skills")
_JSON_COLUMNS = _JSON_LIST_COLUMNS + ("metadata",)
_BOOL_COLUMNS = ("is_easy_apply", "is_already_applied", "has_apply_button")
_NULLABLE_BOOL_COLUMNS = ("experience_match", "location_match")

# Phase 3 application lifecycle, tracked on the jobs row (NULL == never prepared).
# A recommended score only makes a job a candidate; submitting always needs human approval.
APPLICATION_STATES = (
    "not_ready",
    "ready",
    "preparing",
    "review_required",
    "approved",
    "submitting",
    "applied",
    "failed",
    "requires_login",
    "requires_manual_action",
    "already_applied",
    "external_application",
    "job_unavailable",
    "recovery_required",  # Phase 3.3: Apply may have been clicked; only recover-application clears it
)
# Nothing left to do automatically for these jobs.
FINAL_APPLICATION_STATES = ("applied", "already_applied", "external_application", "job_unavailable")
APPLICATION_COLUMNS = {
    "application_state": "TEXT",
    "application_code": "TEXT",  # e.g. AUTH_REQUIRED, EXTERNAL_APPLICATION (see naukri_application.py)
    "application_reason": "TEXT",
    "application_updated_at": "TIMESTAMP",
    "external_apply_url": "TEXT",
}
# Extra application-history fields on applied_jobs (added in place, existing rows keep NULL).
APPLIED_JOBS_EXTRA_COLUMNS = {
    "job_row_id": "INTEGER",
    "resume_path": "TEXT",
    "application_url": "TEXT",
    "failure_reason": "TEXT",
    "external_url": "TEXT",
}
APPLIED_STATUS = "applied"
# applied_jobs.status set by recovery when Naukri shows the job as not applied (the job is requeued).
NOT_APPLIED_STATUS = "not_applied"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applied_jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                platform TEXT NOT NULL,
                url TEXT NOT NULL,
                match_score REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                applied_at TIMESTAMP NOT NULL
            )
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(applied_jobs)")}
        for name, declaration in APPLIED_JOBS_EXTRA_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE applied_jobs ADD COLUMN {name} {declaration}")
        _init_jobs_table(conn)
        _init_attempts_table(conn)
        conn.commit()


def _init_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT 'naukri',
            status TEXT NOT NULL DEFAULT 'discovered',
            first_seen_at TIMESTAMP NOT NULL,
            last_seen_at TIMESTAMP NOT NULL
        )
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    for name, declaration in {**JOB_COLUMNS, **SCORING_COLUMNS, **APPLICATION_COLUMNS}.items():
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError as error:
                # Another process may have completed this idempotent migration
                # between PRAGMA and ALTER.
                if "duplicate column name" not in str(error).lower():
                    raise
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source_job_id ON jobs(source, job_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_match_status ON jobs(match_status)")


def _init_attempts_table(conn: sqlite3.Connection) -> None:
    """Phase 3.3 attempt history: one row per application or recovery attempt (write-ahead).

    apply_clicked is set *before* Naukri's Apply is clicked, so an interrupted run can never hide a
    click. A completed row (completed_at set) is never modified again.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS application_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_row_id INTEGER NOT NULL,
            job_id TEXT,
            kind TEXT NOT NULL DEFAULT 'apply',
            started_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP,
            apply_clicked INTEGER NOT NULL DEFAULT 0,
            state TEXT,
            code TEXT,
            reason TEXT,
            resume_path TEXT,
            resume_version INTEGER,
            application_url TEXT,
            external_url TEXT,
            confirmation_signal TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_job ON application_attempts(job_row_id)")


def _find_job_row_id(
    conn: sqlite3.Connection,
    dedup_key: str,
    job_id: Optional[str] = None,
    url: Optional[str] = None,
    source: str = "naukri",
) -> Optional[int]:
    row = conn.execute("SELECT id FROM jobs WHERE dedup_key = ?", (dedup_key,)).fetchone()
    if not row and job_id:
        row = conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND job_id = ?", (source, job_id)
        ).fetchone()
    if not row and url:
        row = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    return row[0] if row else None


def job_exists(
    dedup_key: str, job_id: Optional[str] = None, url: Optional[str] = None, source: str = "naukri"
) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return _find_job_row_id(conn, dedup_key, job_id, url, source) is not None


def save_discovered_job(job: dict) -> bool:
    """Insert a discovered job. Returns False (and only refreshes last_seen_at) if it already exists."""
    now = datetime.now().isoformat(timespec="seconds")
    source = job.get("source") or "naukri"
    with sqlite3.connect(DB_PATH) as conn:
        existing_id = _find_job_row_id(conn, job["dedup_key"], job.get("job_id"), job.get("url"), source)
        if existing_id is not None:
            conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (now, existing_id))
            return False

        values = {name: job.get(name) for name in JOB_COLUMNS if name in job}
        for name in _JSON_COLUMNS:
            if name in values:
                values[name] = json.dumps(values[name] or ([] if name == "skills" else {}))
        for name in _BOOL_COLUMNS:
            if name in values:
                values[name] = int(bool(values[name]))
        values.update(
            dedup_key=job["dedup_key"],
            source=source,
            status=JOB_STATUS_DISCOVERED,
            first_seen_at=now,
            last_seen_at=now,
        )
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", tuple(values.values()))
        conn.commit()
        return True


def get_jobs(
    status: Optional[str] = JOB_STATUS_DISCOVERED,
    source: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Stored jobs as dicts for downstream matching.

    JSON columns are decoded (lists default to [], metadata to {}), flags become
    bools, and unscored experience_match/location_match stay None.
    """
    query, params = "SELECT * FROM jobs WHERE 1 = 1", []
    if status:
        query += " AND status = ?"
        params.append(status)
    if source:
        query += " AND source = ?"
        params.append(source)
    query += " ORDER BY first_seen_at DESC, id DESC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(query, params)]
    for row in rows:
        for name in _JSON_COLUMNS:
            empty = [] if name in _JSON_LIST_COLUMNS else {}
            row[name] = json.loads(row[name]) if row.get(name) else empty
        for name in _BOOL_COLUMNS:
            row[name] = bool(row.get(name))
        for name in _NULLABLE_BOOL_COLUMNS:
            row[name] = None if row.get(name) is None else bool(row[name])
    return rows


def save_job_score(
    job_row_id: int,
    match_score: float,
    match_status: str,
    match_reason: Optional[str] = None,
    matching_skills: Optional[list[str]] = None,
    missing_skills: Optional[list[str]] = None,
    experience_match: Optional[bool] = None,
    location_match: Optional[bool] = None,
) -> bool:
    """Persist a Phase 2 scoring result for jobs.id and mark the job scored.

    Storage only: validates inputs, never computes a score. Returns False if no such job.
    """
    if not 0 <= float(match_score) <= 100:
        raise ValueError(f"match_score must be between 0 and 100, got {match_score}")
    if match_status not in MATCH_STATUSES:
        raise ValueError(f"match_status must be one of {MATCH_STATUSES}, got {match_status!r}")

    def to_nullable_int(value: Optional[bool]) -> Optional[int]:
        return None if value is None else int(bool(value))

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            UPDATE jobs SET status = ?, match_score = ?, match_status = ?, match_reason = ?,
                matching_skills = ?, missing_skills = ?, experience_match = ?,
                location_match = ?, scored_at = ?
            WHERE id = ?
            """,
            (
                JOB_STATUS_SCORED,
                float(match_score),
                match_status,
                match_reason,
                json.dumps(matching_skills or []),
                json.dumps(missing_skills or []),
                to_nullable_int(experience_match),
                to_nullable_int(location_match),
                datetime.now().isoformat(timespec="seconds"),
                job_row_id,
            ),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_job(job_row_id: int) -> Optional[dict]:
    """One stored job (decoded like get_jobs) by jobs.id, or None."""
    return next((job for job in get_jobs(status=None) if job["id"] == job_row_id), None)


def _application_key(job: dict) -> str:
    """applied_jobs.id for a jobs row: the Naukri job id (as the legacy driver used), else the row id."""
    return str(job.get("job_id") or f"jobrow:{job['id']}")


def has_successful_application(job: dict) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM applied_jobs WHERE id = ? AND status = ?", (_application_key(job), APPLIED_STATUS)
        ).fetchone()
    return row is not None


def get_application_candidates(
    include_review: bool = False, job_row_id: Optional[int] = None, limit: Optional[int] = None
) -> list[dict]:
    """Scored jobs eligible for application preparation (never permission to submit).

    Default: match_status 'recommended' only. Excludes jobs already applied (Naukri flag, a final
    application state, or a successful applied_jobs record) and jobs that need nothing more
    (external application, unavailable). job_row_id narrows to one job but never bypasses the rules.
    """
    wanted = ("recommended", "review") if include_review else ("recommended",)
    candidates = []
    for job in sorted(get_jobs(status=JOB_STATUS_SCORED), key=lambda j: (-(j["match_score"] or 0), j["id"])):
        if job_row_id is not None and job["id"] != job_row_id:
            continue
        if job["match_status"] not in wanted or job["is_already_applied"]:
            continue
        record = get_application_record(job)
        if job.get("application_state") in FINAL_APPLICATION_STATES + ("recovery_required",) \
                or has_successful_application(job) \
                or open_attempts(job["id"]) \
                or (record is not None and record.get("status") not in (NOT_APPLIED_STATUS,)):
            continue
        candidates.append(job)
    return candidates[:limit] if limit else candidates


def set_application_state(
    job_row_id: int,
    state: str,
    code: Optional[str] = None,
    reason: Optional[str] = None,
    external_url: Optional[str] = None,
) -> None:
    if state not in APPLICATION_STATES:
        raise ValueError(f"unknown application state {state!r}")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE jobs SET application_state = ?, application_code = ?, application_reason = ?,
                application_updated_at = ?, external_apply_url = COALESCE(?, external_apply_url)
            WHERE id = ?
            """,
            (state, code, reason, datetime.now().isoformat(timespec="seconds"), external_url, job_row_id),
        )
        conn.commit()


def record_application_attempt(
    job: dict,
    status: str,
    resume_path: Optional[str] = None,
    application_url: Optional[str] = None,
    failure_reason: Optional[str] = None,
    external_url: Optional[str] = None,
) -> bool:
    """Record a genuine submission attempt in applied_jobs (one row per job).

    Idempotent: a job already recorded as applied is never written again (returns False).
    A non-applied earlier attempt (e.g. unconfirmed) is updated in place.
    """
    key = _application_key(job)
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT status FROM applied_jobs WHERE id = ?", (key,)).fetchone()
        if row and row[0] == APPLIED_STATUS:
            return False
        values = (
            job.get("title") or "", job.get("company") or "", "Naukri", job.get("url") or "",
            float(job.get("match_score") or 0), status, now, job["id"], resume_path,
            application_url or job.get("url"), failure_reason, external_url,
        )
        if row:
            conn.execute(
                """
                UPDATE applied_jobs SET title = ?, company = ?, platform = ?, url = ?, match_score = ?,
                    status = ?, applied_at = ?, job_row_id = ?, resume_path = ?, application_url = ?,
                    failure_reason = ?, external_url = ?
                WHERE id = ?
                """,
                (*values, key),
            )
        else:
            conn.execute(
                """
                INSERT INTO applied_jobs (title, company, platform, url, match_score, status, applied_at,
                    job_row_id, resume_path, application_url, failure_reason, external_url, id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, key),
            )
        conn.commit()
        return True


def start_attempt(job: dict, kind: str = "apply", resume_path: Optional[str] = None,
                  resume_version: Optional[int] = None) -> int:
    now = datetime.now().isoformat(timespec="milliseconds")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO application_attempts (job_row_id, job_id, kind, started_at, state, resume_path,
                resume_version, application_url, created_at)
            VALUES (?, ?, ?, ?, 'started', ?, ?, ?, ?)
            """,
            (job["id"], job.get("job_id"), kind, now, resume_path, resume_version, job.get("url"), now),
        )
        conn.commit()
        return cursor.lastrowid


def mark_apply_clicked(attempt_id: int, clicked: bool = True) -> None:
    """Set immediately before clicking Apply (and cleared only when the click provably never happened)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE application_attempts SET apply_clicked = ? WHERE id = ? AND completed_at IS NULL",
                     (int(clicked), attempt_id))
        conn.commit()


def finish_attempt(attempt_id: int, state: str, code: Optional[str] = None, reason: Optional[str] = None,
                   success: bool = False, confirmation_signal: Optional[str] = None,
                   external_url: Optional[str] = None) -> bool:
    """Complete an attempt once. Returns False when it was already completed (rows are immutable then)."""
    now = datetime.now()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT started_at FROM application_attempts WHERE id = ? AND completed_at IS NULL",
                           (attempt_id,)).fetchone()
        if row is None:
            return False
        try:
            duration = int((now - datetime.fromisoformat(row[0])).total_seconds() * 1000)
        except ValueError:
            duration = None
        conn.execute(
            """
            UPDATE application_attempts SET completed_at = ?, state = ?, code = ?, reason = ?, success = ?,
                confirmation_signal = ?, external_url = ?, duration_ms = ?
            WHERE id = ? AND completed_at IS NULL
            """,
            (now.isoformat(timespec="milliseconds"), state, code, reason, int(bool(success)), confirmation_signal,
             external_url, duration, attempt_id),
        )
        conn.commit()
        return True


def get_attempts(job_row_id: int) -> list[dict]:
    """All attempts for a job, oldest first."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM application_attempts WHERE job_row_id = ? ORDER BY id", (job_row_id,))
        return [dict(r) for r in rows]


def open_attempts(job_row_id: int) -> list[dict]:
    return [a for a in get_attempts(job_row_id) if not a["completed_at"]]


def resolve_unconfirmed_record(job: dict, reason: str) -> bool:
    """Recovery saw Naukri's Apply button: mark a non-applied history row as not_applied. An applied
    row is never touched. Returns True when a row was changed."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "UPDATE applied_jobs SET status = ?, failure_reason = ? WHERE id = ? AND status != ?",
            (NOT_APPLIED_STATUS, reason, _application_key(job), APPLIED_STATUS),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_jobs_by_application_state(states: tuple, limit: Optional[int] = None) -> list[dict]:
    jobs = [j for j in get_jobs(status=None) if j.get("application_state") in states]
    jobs.sort(key=lambda j: (str(j.get("application_updated_at") or ""), j["id"]), reverse=True)
    return jobs[:limit] if limit else jobs


def get_application_record(job: dict) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM applied_jobs WHERE id = ?", (_application_key(job),)).fetchone()
    return dict(row) if row else None


def is_job_applied(job_id: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT 1 FROM applied_jobs WHERE id = ?", (job_id,))
        return cursor.fetchone() is not None


def log_job_application(job_data: dict) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO applied_jobs
            (id, title, company, platform, url, match_score, status, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_data["id"],
                job_data["title"],
                job_data["company"],
                job_data["platform"],
                job_data["url"],
                job_data["match_score"],
                job_data.get("status", "pending"),
                job_data.get("applied_at", datetime.now().isoformat()),
            ),
        )
        conn.commit()


def get_applied_jobs_count(date: Optional[str] = None) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        if date:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM applied_jobs WHERE DATE(applied_at) = ?", (date,)
            )
        else:
            cursor = conn.execute("SELECT COUNT(*) FROM applied_jobs")
        return cursor.fetchone()[0]


def close_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


__all__ = [
    "init_db",
    "is_job_applied",
    "log_job_application",
    "get_applied_jobs_count",
    "close_db",
    "job_exists",
    "save_discovered_job",
    "get_jobs",
    "save_job_score",
    "JOB_STATUS_DISCOVERED",
    "JOB_STATUS_SCORED",
    "MATCH_STATUSES",
    "APPLICATION_STATES",
    "get_job",
    "get_application_candidates",
    "set_application_state",
    "record_application_attempt",
    "get_application_record",
    "has_successful_application",
    "start_attempt",
    "mark_apply_clicked",
    "finish_attempt",
    "get_attempts",
    "open_attempts",
    "resolve_unconfirmed_record",
    "get_jobs_by_application_state",
]
