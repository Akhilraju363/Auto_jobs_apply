"""File handoff between Auto_job_apply and the Job Application AI Agent (Phase 2).

export_discovered_jobs(): discovered jobs -> exports/naukri_jobs.json in the AI Agent's raw-job format.
import_agent_scores():    AI Agent output/scored_jobs.json -> db.save_job_score().

The AI Agent owns scoring, tailoring, research and the tracker. This module only moves data:
it never scores, never calls an LLM and never writes into the AI Agent's files.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

from config import AI_AGENT_EXPORT_LIMIT, AI_AGENT_OUTPUT_DIR, BASE_DIR, NAUKRI_EXPORT_PATH
from db import JOB_STATUS_DISCOVERED, get_jobs, init_db, save_job_score

logger = logging.getLogger(__name__)

AGENT_SOURCE = "Naukri"
# Mirrors the AI Agent's limits in scripts/jd_analysis.py (MIN_JD_CHARS, MAX_JD_CHARS, MAX_FIELD_CHARS).
MIN_DESCRIPTION_CHARS = 80
MAX_DESCRIPTION_CHARS = 30_000
MAX_FIELD_CHARS = 160
AGENT_QUALIFY_CUTOFF = 8  # the AI Agent's QUALIFY_CUTOFF; informational, never applied to the Agent
REVIEW_MIN_SCORE = 6

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG = re.compile(r"<[^>]{1,200}>")


def agent_canonical_link(url: Optional[str]) -> Optional[str]:
    """Same rule as the AI Agent's scripts/job_links.py canonical_link(): drop query and fragment."""
    if not url:
        return url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def agent_description_length(text: str) -> int:
    """Length after the AI Agent's sanitize_jd() normalization (what its 80-char check measures)."""
    s = _CTRL.sub("", str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    s = _TAG.sub(" ", s)
    s = re.sub(r"[ \t]+", " ", s)
    return len(re.sub(r"\n{3,}", "\n\n", s).strip())


def build_export_description(job: dict) -> tuple[Optional[str], bool]:
    """(description to export, supplemented?). Short JDs get Naukri's own key skills appended;
    nothing else is ever added. None if still too short."""
    description = (job.get("job_description") or "").strip()
    if agent_description_length(description) >= MIN_DESCRIPTION_CHARS:
        return description, False
    skills = [s for s in job.get("skills") or [] if str(s).strip()]
    if skills:
        supplemented = f"{description}\n\nKey skills:\n{', '.join(skills)}".strip()
        if agent_description_length(supplemented) >= MIN_DESCRIPTION_CHARS:
            return supplemented, True
    return None, False


def to_agent_record(job: dict) -> tuple[Optional[dict], str]:
    """Map a stored job to the AI Agent raw-job record, or (None, reason) if ineligible."""
    title, company = (job.get("title") or "").strip(), (job.get("company") or "").strip()
    link = agent_canonical_link((job.get("url") or "").strip())
    if not title:
        return None, "missing title"
    if not company:
        return None, "missing company"
    if not link or not link.startswith(("http://", "https://")):
        return None, "missing or invalid URL"
    if len(title) > MAX_FIELD_CHARS or len(company) > MAX_FIELD_CHARS:
        return None, f"title/company longer than {MAX_FIELD_CHARS} characters"
    description, supplemented = build_export_description(job)
    if description is None:
        return None, f"description shorter than {MIN_DESCRIPTION_CHARS} characters even with key skills"
    if agent_description_length(description) > MAX_DESCRIPTION_CHARS:
        return None, f"description longer than {MAX_DESCRIPTION_CHARS} characters"
    record = {
        "title": title,
        "company": company,
        "link": link,
        "description": description,
        "posted_date": job.get("posted_date"),
        "location": job.get("location") or "",
        "source": AGENT_SOURCE,
        "found_at": (job.get("first_seen_at") or job.get("scraped_at") or "")[:10] or None,
        "source_job_id": job.get("job_id"),
        "dedup_key": job.get("dedup_key"),
        "skills": list(job.get("skills") or []),
    }
    return record, "supplemented with key skills" if supplemented else ""


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass
class ExportResult:
    path: Path
    limit: int
    discovered: int = 0
    eligible: int = 0
    supplemented: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (job reference, reason)
    exported: list[dict] = field(default_factory=list)
    filtered_out: int = 0  # rejected by export_discovered_jobs(job_filter=...)


def export_discovered_jobs(
    path: Optional[Path] = None,
    limit: Optional[int] = None,
    job_filter: Optional[Callable[[dict], bool]] = None,
) -> ExportResult:
    """Write up to `limit` eligible discovered jobs (oldest first) for the AI Agent to ingest.

    Only status='discovered' jobs are selected, so scored jobs are never resent. The file is
    rewritten each time with the current pending jobs; jobs.db is not modified.
    job_filter (Phase 4 pipeline): only jobs it accepts are exported, e.g. fresh, never-applied jobs,
    so stale jobs never spend the small per-run AI Agent budget.
    """
    init_db()
    result = ExportResult(path=Path(path or NAUKRI_EXPORT_PATH), limit=limit or AI_AGENT_EXPORT_LIMIT)
    jobs = sorted(get_jobs(status=JOB_STATUS_DISCOVERED), key=lambda j: j["id"])
    result.discovered = len(jobs)
    if job_filter is not None:
        kept = [job for job in jobs if job_filter(job)]
        result.filtered_out = len(jobs) - len(kept)
        jobs = kept
    eligible = []
    for job in jobs:
        record, note = to_agent_record(job)
        reference = f"{job.get('title') or '?'} @ {job.get('company') or '?'} [{job.get('job_id') or job['id']}]"
        if record is None:
            result.skipped.append((reference, note))
            logger.warning(f"Not exporting {reference}: {note}")
            continue
        if note:
            result.supplemented += 1
            logger.info(f"Export description for {reference} {note}")
        eligible.append(record)
    result.eligible = len(eligible)
    result.exported = eligible[: result.limit]
    _write_json_atomic(result.path, result.exported)
    logger.info(f"Exported {len(result.exported)} of {result.eligible} eligible jobs to {result.path}")
    return result


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def classify_score(score: int) -> str:
    """Auto_job_apply's label for an AI Agent 1-10 score. The Agent's own 8+ cutoff is untouched."""
    if score >= AGENT_QUALIFY_CUTOFF:
        return "recommended"
    if score >= REVIEW_MIN_SCORE:
        return "review"
    return "skipped"


def parse_agent_result(entry: Any) -> dict:
    """Validate one scored_jobs.json entry; raises ValueError when malformed."""
    if not isinstance(entry, dict):
        raise ValueError("entry is not an object")
    score = entry.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or score != int(score):
        raise ValueError(f"score is not an integer: {score!r}")
    score = int(score)
    if not 1 <= score <= 10:
        raise ValueError(f"score out of range 1-10: {score}")
    reasoning = entry.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ValueError("reasoning is not a string")
    lists = {}
    for name in ("matched_must_haves", "missing_must_haves"):
        value = entry.get(name) or []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{name} is not a list of strings")
        lists[name] = value
    return {
        "score": score,
        "reasoning": (reasoning or "").strip() or None,
        "matched": lists["matched_must_haves"],
        "missing": lists["missing_must_haves"],
    }


@dataclass
class ImportResult:
    path: Path
    results_read: int = 0
    matched: int = 0
    scored_now: int = 0
    already_scored: int = 0
    unmatched: int = 0  # results for jobs not in jobs.db (e.g. LinkedIn jobs)
    malformed: int = 0
    by_status: dict[str, int] = field(default_factory=lambda: {"recommended": 0, "review": 0, "skipped": 0})
    still_discovered: int = 0


def _index_jobs() -> tuple[dict[str, dict], dict[str, dict]]:
    jobs = get_jobs(status=None)
    by_link = {agent_canonical_link(j["url"]): j for j in jobs if j.get("url")}
    by_key = {j["dedup_key"]: j for j in jobs if j.get("dedup_key")}
    return by_link, by_key


def import_agent_scores(scored_path: Optional[Path] = None) -> ImportResult:
    """Copy AI Agent scores into jobs.db. Idempotent: already-scored jobs are never overwritten.

    Jobs without a result (not scored yet, or failed in the Agent) stay 'discovered' and are
    exported again next time.
    """
    if scored_path is None:
        if AI_AGENT_OUTPUT_DIR is None:
            raise ValueError("AI_AGENT_OUTPUT_DIR is not set in .env (path to the AI Agent's output folder)")
        scored_path = AI_AGENT_OUTPUT_DIR / "scored_jobs.json"
    scored_path = Path(scored_path)
    result = ImportResult(path=scored_path)
    init_db()
    if not scored_path.exists():
        raise FileNotFoundError(f"AI Agent scores not found: {scored_path} (has the AI Agent run?)")
    entries = json.loads(scored_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"{scored_path} does not contain a list of scored jobs")

    by_link, by_key = _index_jobs()
    for index, entry in enumerate(entries):
        result.results_read += 1
        link = entry.get("link") if isinstance(entry, dict) else None
        key = entry.get("dedup_key") if isinstance(entry, dict) else None
        job = by_link.get(agent_canonical_link(link)) if link else None
        job = job or (by_key.get(key) if key else None)
        if job is None:
            result.unmatched += 1
            continue
        result.matched += 1
        if job.get("scored_at") or job.get("status") != JOB_STATUS_DISCOVERED:
            result.already_scored += 1
            continue
        try:
            parsed = parse_agent_result(entry)
        except ValueError as e:
            result.malformed += 1
            logger.warning(f"Malformed AI Agent result #{index} for {job['url']}: {e}")
            continue
        status = classify_score(parsed["score"])
        saved = save_job_score(
            job["id"],
            parsed["score"] * 10,
            status,
            match_reason=parsed["reasoning"],
            matching_skills=parsed["matched"],
            missing_skills=parsed["missing"],
            experience_match=None,
            location_match=None,
        )
        if saved:
            job["status"], job["scored_at"] = "scored", datetime.now().isoformat()
            result.scored_now += 1
            result.by_status[status] += 1
            logger.info(f"Imported score {parsed['score']}/10 ({status}) for {job['title']} @ {job['company']}")
    result.still_discovered = len(get_jobs(status=JOB_STATUS_DISCOVERED))
    return result


# ---------------------------------------------------------------------------
# Running the AI Agent (Phase 4 pipeline)
# ---------------------------------------------------------------------------

AGENT_NOT_CONFIGURED = "AGENT_NOT_CONFIGURED"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
AGENT_STAGE_FAILED = "AGENT_STAGE_FAILED"
AGENT_TIMEOUT = "AGENT_TIMEOUT"


@dataclass
class AgentRunResult:
    ok: bool
    stages_run: list[str] = field(default_factory=list)
    failed_stage: Optional[str] = None
    code: Optional[str] = None
    message: str = ""


def resolve_agent_python(agent_dir: Optional[Path], configured: Optional[Path] = None) -> Optional[Path]:
    """The AI Agent's own interpreter: AI_AGENT_PYTHON, else its .venv. None when neither exists."""
    if configured:
        return Path(configured) if Path(configured).exists() else None
    if agent_dir is None:
        return None
    for candidate in (Path(agent_dir) / ".venv" / "Scripts" / "python.exe", Path(agent_dir) / ".venv" / "bin" / "python"):
        if candidate.exists():
            return candidate
    return None


def agent_environment(export_path: Path, base_env: Optional[dict] = None,
                      own_env_file: Path = BASE_DIR / ".env") -> dict:
    """Environment for the AI Agent's scripts.

    Every key that came from this project's .env is removed first: the Agent loads its own .env
    with python-dotenv, which never overrides existing variables, so ours (e.g. GEMINI_API_KEY)
    would otherwise silently replace the Agent's. Then the Naukri handoff is pointed at our export
    and JOB_SOURCES is limited to naukri, so this pipeline never triggers a paid LinkedIn scrape.
    """
    env = dict(os.environ if base_env is None else base_env)
    own = dotenv_values(own_env_file) if Path(own_env_file).exists() else {}
    for name in own:
        env.pop(name, None)
    env["NAUKRI_JOBS_PATH"] = str(Path(export_path).resolve())
    env["JOB_SOURCES"] = "naukri"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_agent_stages(
    agent_dir: Optional[Path],
    stages: list[str],
    export_path: Optional[Path] = None,
    python: Optional[Path] = None,
    timeout_minutes: int = 60,
    runner: Callable[..., Any] = subprocess.run,
) -> AgentRunResult:
    """Run the AI Agent's own stage scripts (scripts/<stage>) in its folder, in order, stopping at
    the first failure. This is the same file handoff as before -- the Agent reads NAUKRI_JOBS_PATH
    and writes output/scored_jobs.json -- just started by the pipeline instead of by hand."""
    if agent_dir is None:
        return AgentRunResult(False, code=AGENT_NOT_CONFIGURED,
                              message="AI_AGENT_DIR / AI_AGENT_OUTPUT_DIR is not set in .env")
    agent_dir = Path(agent_dir)
    interpreter = python or resolve_agent_python(agent_dir)
    if interpreter is None or not Path(interpreter).exists():
        return AgentRunResult(False, code=AGENT_NOT_FOUND,
                              message=f"AI Agent Python not found (expected {agent_dir / '.venv'}; set AI_AGENT_PYTHON)")
    env = agent_environment(Path(export_path or NAUKRI_EXPORT_PATH))
    result = AgentRunResult(True)
    for stage in stages:
        script = agent_dir / "scripts" / stage
        if not script.exists():
            return AgentRunResult(False, result.stages_run, stage, AGENT_NOT_FOUND, f"AI Agent script not found: {script}")
        logger.info(f"AI Agent stage started: {stage}")
        try:
            completed = runner([str(interpreter), str(script)], cwd=str(agent_dir), env=env,
                               timeout=timeout_minutes * 60, check=False)
        except subprocess.TimeoutExpired:
            return AgentRunResult(False, result.stages_run, stage, AGENT_TIMEOUT,
                                  f"{stage} did not finish within {timeout_minutes} minutes")
        except OSError as e:
            return AgentRunResult(False, result.stages_run, stage, AGENT_NOT_FOUND, f"could not start {stage}: {e}")
        if completed.returncode != 0:
            return AgentRunResult(False, result.stages_run, stage, AGENT_STAGE_FAILED,
                                  f"{stage} exited with code {completed.returncode}")
        result.stages_run.append(stage)
        logger.info(f"AI Agent stage finished: {stage}")
    return result


__all__ = [
    "AgentRunResult",
    "agent_environment",
    "resolve_agent_python",
    "run_agent_stages",
    "ExportResult",
    "ImportResult",
    "agent_canonical_link",
    "build_export_description",
    "classify_score",
    "export_discovered_jobs",
    "import_agent_scores",
    "parse_agent_result",
    "to_agent_record",
]
