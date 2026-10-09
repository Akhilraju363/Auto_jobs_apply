"""Deterministic Phase 3.4 job eligibility and prioritization.

This module does not browse, score, submit, or answer application questions.
It only evaluates stored job data and explicit applicant preferences.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

FRESH = "FRESH"
STALE = "STALE"
UNKNOWN = "UNKNOWN"

_RELATIVE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(minute|min|hour|hr)s?\s*ago\s*$", re.I)
_DAY = re.compile(r"^\s*(\d+)\s*day[s]?\s*ago\s*$", re.I)
_ALIASES = {
    "bangalore": "bangalore", "bengaluru": "bangalore",
    "chennai": "chennai",
    "remote": "remote", "wfh": "remote", "work from home": "remote",
}


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None and value.utcoffset() is not None else None


def _parse_aware(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def freshness_for(
    label: Any = None,
    *,
    reference_time: Optional[datetime] = None,
    posted_at: Optional[datetime] = None,
    updated_at: Optional[datetime] = None,
    window_hours: int = 24,
) -> tuple[str, Optional[float]]:
    """Return ``(status, age_hours)``; ambiguous or timezone-naive data is UNKNOWN."""
    reference = _aware(reference_time)
    if reference is None or window_hours <= 0:
        return UNKNOWN, None
    timestamp = _parse_aware(updated_at) or _parse_aware(posted_at)
    if timestamp is not None:
        age = (reference - timestamp).total_seconds() / 3600
        return (FRESH if 0 <= age < window_hours else STALE), age
    text = str(label or "").strip().lower()
    if text in {"just now", "now", "today", "few hours ago"}:
        return FRESH, 0.0
    match = _RELATIVE.match(text)
    if match:
        age = float(match.group(1)) / 60 if match.group(2).lower().startswith("min") else float(match.group(1))
        return (FRESH if age < window_hours else STALE), age
    day = _DAY.match(text)
    if day:
        # Naukri's "1 day ago" has no exact hour and must not be treated as 24 hours.
        age = float(day.group(1)) * 24
        return (UNKNOWN if age == 24 else (FRESH if age < window_hours else STALE)), age
    return UNKNOWN, None


def _scraped_time(value: Any) -> Optional[datetime]:
    """When the label was read. The scanner stores scraped_at as naive local time."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if _aware(value) else value.astimezone()


def job_freshness(job: dict, *, reference_time: Optional[datetime], window_hours: int = 24
                  ) -> tuple[str, Optional[float]]:
    """Freshness of a stored job. Exact posted/updated timestamps win. A relative label ("5 hours
    ago") describes the moment it was read, so it is aged by the time since scraped_at: a job read
    as "Today" three days ago is STALE now. Ambiguous labels ("1 day ago") stay UNKNOWN."""
    posted_at, updated_at = job.get("posted_at"), job.get("updated_at")
    reference, scraped = _aware(reference_time), _scraped_time(job.get("scraped_at"))
    if _parse_aware(posted_at) or _parse_aware(updated_at) or reference is None or scraped is None:
        return freshness_for(job.get("posted_label"), reference_time=reference_time, posted_at=posted_at,
                             updated_at=updated_at, window_hours=window_hours)
    status, age = freshness_for(job.get("posted_label"), reference_time=scraped, window_hours=window_hours)
    if status == UNKNOWN or age is None:
        return UNKNOWN, None
    age += max(0.0, (reference - scraped).total_seconds() / 3600)
    return (FRESH if age < window_hours else STALE), age


def normalize_preferred_location(value: Any) -> str:
    value = re.sub(r"[\s\W_]+", " ", str(value or "").strip().lower()).strip()
    return _ALIASES.get(value, value)


def location_match(location: Any, preferred: list[str]) -> Optional[bool]:
    configured = {normalize_preferred_location(v) for v in preferred if str(v).strip()}
    if not configured:
        return None
    values = {normalize_preferred_location(v) for v in re.split(r",|/|\||;", str(location or "")) if v.strip()}
    if not values:
        return None
    return bool(values & configured)


def experience_match(job: dict, applicant_experience: Optional[float]) -> Optional[bool]:
    if applicant_experience is None:
        return None
    low, high = job.get("experience_min"), job.get("experience_max")
    if low is None and high is None:
        text = str(job.get("experience_text") or "")
        numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
        if not numbers:
            return None
        low, high = numbers[0], numbers[-1]
    return applicant_experience >= low and (high is None or applicant_experience <= high)


def _score_10(value: Any) -> Optional[float]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score / 10 if score > 10 else score


@dataclass(frozen=True)
class Eligibility:
    freshness_status: str
    location_match: Optional[bool]
    experience_match: Optional[bool]
    score: Optional[float]
    resume_ready: bool
    eligible: bool
    reason: str


def evaluate_job(
    job: dict,
    *,
    reference_time: datetime,
    preferred_locations: list[str],
    applicant_experience: Optional[float] = None,
    resume_ready: bool = True,
    score_threshold: float = 8,
    window_hours: int = 24,
) -> Eligibility:
    freshness, _ = job_freshness(job, reference_time=reference_time, window_hours=window_hours)
    loc = job.get("location_match")
    loc = bool(loc) if loc is not None else location_match(job.get("location"), preferred_locations)
    exp = job.get("experience_match")
    exp = bool(exp) if exp is not None else experience_match(job, applicant_experience)
    score = _score_10(job.get("match_score"))
    blocked = job.get("application_state") in {
        "recovery_required", "already_applied", "applied", "external_application", "job_unavailable",
    } or job.get("is_already_applied") or job.get("history_applied")
    eligible = (
        freshness == FRESH and score is not None and score >= score_threshold
        and loc is not False and exp is not False and resume_ready and not blocked
    )
    if blocked:
        reason = "application history blocks this job"
    elif freshness != FRESH:
        reason = f"freshness is {freshness}"
    elif score is None or score < score_threshold:
        reason = "score is below the application threshold"
    elif loc is False:
        reason = "location is not preferred"
    elif exp is False:
        reason = "experience does not match"
    elif not resume_ready:
        reason = "verified resume is not ready"
    else:
        reason = "ready for human review"
    return Eligibility(freshness, loc, exp, score, resume_ready, eligible, reason)


def priority_score(job: dict, result: Eligibility) -> int:
    score = int(round((result.score or 0) * 10))
    return max(0, min(100, score * 7 + (20 if result.location_match is True else 0)
                       + (10 if result.experience_match is True else 0)
                       + (3 if result.resume_ready else 0)))


__all__ = [
    "FRESH", "STALE", "UNKNOWN", "Eligibility", "freshness_for", "job_freshness",
    "normalize_preferred_location", "location_match", "experience_match",
    "evaluate_job", "priority_score",
]
