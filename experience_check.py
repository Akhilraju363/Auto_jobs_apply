"""Strict experience eligibility: the JD's minimum required years vs the master resume.

The master ATS resume (MASTER_RESUME_PATH, the AI Agent's base_resume.md) is the only source of
the applicant's experience. Its explicit claim ("4+ years of experience") is read as the floor
(4 years) and is never rounded up or inflated; APPLICANT_TOTAL_EXPERIENCE can only lower it.

A job's requirement comes from Naukri's structured experience field (authoritative) and from
explicit mentions in the title and description ("5-8 years", "Minimum 4 years", "3+ yrs of
experience"). Ranges are compared by their minimum, never their maximum.

    ELIGIBLE         every stated minimum <= verified years
    INELIGIBLE       Naukri's minimum (or every stated minimum) > verified years
    REVIEW_REQUIRED  no requirement found, sources disagree across the verified years, or the
                     resume states no experience: never applied automatically

Pure functions: no browser, no LLM, no network. Runs before export/scoring/tailoring so jobs
that fail cost no API calls.
"""

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from config import MASTER_RESUME_PATH

ELIGIBLE = "ELIGIBLE"
INELIGIBLE = "INELIGIBLE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
# application_code on a job the application layer refused (application_state stays "not_ready")
EXPERIENCE_INELIGIBLE = "EXPERIENCE_INELIGIBLE"
EXPERIENCE_REVIEW_REQUIRED = "EXPERIENCE_REVIEW_REQUIRED"
EXPERIENCE_CODES = (EXPERIENCE_INELIGIBLE, EXPERIENCE_REVIEW_REQUIRED)

_MAX_PLAUSIBLE_YEARS = 40  # "511 Years" is a scraped "5–11" whose dash was lost: not a requirement
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15}
_NUM = r"\d{1,3}(?:\.\d+)?"
# Range separators include U+FFFD: the scraper sometimes stores an en dash as the replacement char.
_SEP = r"\s*(?:-|–|—|~|�|\bto\b)\s*"
_UNIT = r"(?:years?|yrs?)\b"
_MENTION = re.compile(
    rf"(?P<prefix>\b(?:minimum|min\.?|at\s*least|atleast)\s*(?:of\s*)?)?"
    rf"(?<![\d.])(?P<low>{_NUM})(?:{_SEP}(?P<high>{_NUM}))?\s*(?P<plus>\+|plus)?\s*{_UNIT}",
    re.I,
)
_UP_TO = re.compile(r"\b(?:up\s*to|upto|max(?:imum)?|less\s*than|under)\s*(?:of\s*)?$", re.I)
_CONTEXT = re.compile(r"\bexp(?:erience|\.)?\b|\bexperienced\b", re.I)
_RESUME_CLAIM = re.compile(
    rf"(?P<years>{_NUM})\s*(?P<plus>\+)?\s*{_UNIT}\s*(?:of\s+)?(?:\w+\s+){{0,2}}?experience", re.I)


@dataclass(frozen=True)
class VerifiedExperience:
    years: float  # the floor the resume supports: "4+ years" -> 4.0
    text: str  # as shown in reasons: "4+ years"
    source: str = ""


@dataclass(frozen=True)
class Requirement:
    min_years: float
    max_years: Optional[float]
    source: str  # "naukri", "title" or "description"
    snippet: str = ""

    @property
    def text(self) -> str:
        if self.max_years is not None and self.max_years != self.min_years:
            return f"{_fmt(self.min_years)}–{_fmt(self.max_years)} years"
        return f"{_fmt(self.min_years)}+ years"


@dataclass(frozen=True)
class ExperienceCheck:
    status: str
    required_text: Optional[str]  # e.g. "5–8 years"; None when nothing was found
    required_min: Optional[float]
    verified_text: Optional[str]
    verified_years: Optional[float]
    reason: str

    @property
    def match(self) -> Optional[bool]:
        """True / False / None (review) for the legacy experience_match flag."""
        return {ELIGIBLE: True, INELIGIBLE: False}.get(self.status)


def _fmt(value: float) -> str:
    return f"{value:g}"


def _number(value: Optional[str]) -> Optional[float]:
    return None if value is None else float(value)


def _words_to_digits(text: str) -> str:
    return re.sub(r"\b(" + "|".join(_WORDS) + r")\b(?=\s*(?:\+|plus)?\s*(?:-|–|to)?\s*\d*\s*(?:years?|yrs?)\b)",
                  lambda m: str(_WORDS[m.group(1).lower()]), text, flags=re.I)


def find_requirements(text: Any, source: str, *, need_context: bool = True) -> list[Requirement]:
    """Explicit year requirements in free text. With need_context, a mention counts only when
    'experience'/'exp' or 'minimum'/'at least' is next to it, so "a 25-year-old company" or a
    "Senior Level | 5 – 10 Yrs" table row is not read as this job's requirement. "Up to N years"
    states no minimum and is ignored. Implausible numbers (> 40) are dropped as garbled."""
    text = _words_to_digits(re.sub(r"\s+", " ", str(text or "")))
    found = []
    for m in _MENTION.finditer(text):
        before, after = text[max(0, m.start() - 40):m.start()], text[m.end():m.end() + 45]
        if not m.group("prefix") and _UP_TO.search(before.rstrip()):
            continue
        if need_context and not m.group("prefix") and not (
                _CONTEXT.search(after) or _CONTEXT.search(before[-25:])):
            continue
        low, high = float(m.group("low")), _number(m.group("high"))
        if high is not None and high < low:
            low, high = high, low
        if low > _MAX_PLAUSIBLE_YEARS or (high is not None and high > _MAX_PLAUSIBLE_YEARS):
            continue
        found.append(Requirement(low, high, source, m.group(0).strip()))
    return found


def structured_requirement(job: Mapping) -> Optional[Requirement]:
    """Naukri's own experience field ("5 - 8 years"), already parsed into experience_min/max."""
    low, high = job.get("experience_min"), job.get("experience_max")
    if low is not None:
        try:
            low = float(low)
            high = None if high is None else float(high)
        except (TypeError, ValueError):
            return None
        if 0 <= low <= _MAX_PLAUSIBLE_YEARS:
            return Requirement(low, high, "naukri", str(job.get("experience_text") or "").strip())
    text = str(job.get("experience_text") or "")
    if re.search(r"\bfresher\b", text, re.I) and not re.search(r"\d", text):
        return Requirement(0, 0, "naukri", text.strip())
    mentions = find_requirements(text, "naukri", need_context=False)
    return mentions[0] if mentions else None


def job_requirements(job: Mapping) -> list[Requirement]:
    """Every explicit requirement for the job: Naukri's field first, then title and description."""
    structured = structured_requirement(job)
    mentions = find_requirements(job.get("title"), "title", need_context=False) \
        + find_requirements(job.get("job_description"), "description")
    if structured is not None and structured.max_years is not None:
        # "Experience: 35 Years" next to Naukri's "3 - 5 years" is that range with its dash lost in
        # scraping. Only an exact concatenation is dropped; any other odd number stays and is reviewed.
        lost_dash = f"{_fmt(structured.min_years)}{_fmt(structured.max_years)}"
        mentions = [m for m in mentions if m.max_years is not None or "+" in m.snippet
                    or _fmt(m.min_years) != lost_dash]
    return ([structured] if structured else []) + mentions


def verified_resume_experience(path: Optional[Path] = MASTER_RESUME_PATH,
                               environ: Optional[Mapping[str, str]] = None) -> Optional[VerifiedExperience]:
    """The experience the master resume explicitly states, or None (then every job needs review).

    "4+ years of experience" -> 4.0 / "4+ years". Several different claims -> the lowest.
    APPLICANT_TOTAL_EXPERIENCE may lower the value but never raises it above the resume."""
    try:
        text = Path(path).read_text(encoding="utf-8") if path else ""
    except OSError:
        return None
    claims = [(float(m.group("years")), bool(m.group("plus"))) for m in _RESUME_CLAIM.finditer(text)
              if 0 < float(m.group("years")) <= _MAX_PLAUSIBLE_YEARS]
    if not claims:
        return None
    years, plus = min(claims)
    verified = VerifiedExperience(years, f"{_fmt(years)}{'+' if plus else ''} years", str(path))
    environ = os.environ if environ is None else environ
    declared = re.match(_NUM, (environ.get("APPLICANT_TOTAL_EXPERIENCE") or "").strip())
    if declared and float(declared.group()) < years:
        declared_years = float(declared.group())
        return VerifiedExperience(declared_years, f"{_fmt(declared_years)} years", "APPLICANT_TOTAL_EXPERIENCE")
    return verified


def check_experience(job: Mapping, verified: Optional[VerifiedExperience]) -> ExperienceCheck:
    requirements = job_requirements(job)
    structured = next((r for r in requirements if r.source == "naukri"), None)
    shown = structured or (min(requirements, key=lambda r: r.min_years) if requirements else None)
    required_text = shown.text if shown else None
    required_min = shown.min_years if shown else None

    def result(status: str, reason: str, text: Optional[str] = required_text,
               minimum: Optional[float] = required_min) -> ExperienceCheck:
        return ExperienceCheck(status, text, minimum, verified.text if verified else None,
                               verified.years if verified else None, reason)

    if verified is None:
        return result(REVIEW_REQUIRED, "Review required: the master resume states no total experience "
                                       "(expected e.g. '4+ years of experience'), so it cannot be verified.")
    supports = f"resume supports {verified.text}"
    if not requirements:
        return result(REVIEW_REQUIRED, f"Review required: no experience requirement found in the JD; {supports}.")
    above = [r for r in requirements if r.min_years > verified.years]
    if structured is not None and structured.min_years > verified.years:
        return result(INELIGIBLE, f"Skipped: JD requires {structured.text}; {supports}.")
    if len(above) == len(requirements):
        lowest = min(above, key=lambda r: r.min_years)
        return result(INELIGIBLE, f"Skipped: JD requires {lowest.text}; {supports}.", lowest.text, lowest.min_years)
    if "job_description" in job and not str(job.get("job_description") or "").strip():
        # The description was looked for and is empty: its requirements were never seen, so a passing
        # Naukri field alone is not enough. (A dict without the key, e.g. a search card, is not affected.)
        return result(REVIEW_REQUIRED, f"Review required: the job description could not be loaded, so its "
                                       f"experience requirement is unverified; {supports}.")
    if above:
        listed = ", ".join(dict.fromkeys(r.text for r in requirements))
        strictest = max(above, key=lambda r: r.min_years)
        return result(REVIEW_REQUIRED, f"Review required: conflicting experience requirements in the JD ({listed}); "
                                       f"{supports}.", f"{required_text} / {strictest.text}" if required_text
                      else strictest.text, strictest.min_years)
    strictest = max(requirements, key=lambda r: r.min_years)
    text = required_text if structured else strictest.text
    return result(ELIGIBLE, f"Experience OK: JD requires {text}; {supports}.", text,
                  structured.min_years if structured else strictest.min_years)


def record_experience_check(job: Mapping, verified: Optional[VerifiedExperience]) -> ExperienceCheck:
    """check_experience() and store the result on the jobs row (job tracker)."""
    from db import save_experience_check  # local import: db is not needed for the pure check

    check = check_experience(job, verified)
    if job.get("id") is not None:
        save_experience_check(job["id"], check, checked_at=datetime.now().isoformat(timespec="seconds"))
    return check


__all__ = [
    "ELIGIBLE", "INELIGIBLE", "REVIEW_REQUIRED", "EXPERIENCE_INELIGIBLE", "EXPERIENCE_REVIEW_REQUIRED",
    "EXPERIENCE_CODES", "VerifiedExperience", "Requirement", "ExperienceCheck",
    "find_requirements", "structured_requirement", "job_requirements", "verified_resume_experience",
    "check_experience", "record_experience_check",
]
