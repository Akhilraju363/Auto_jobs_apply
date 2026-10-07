"""The applicant's explicitly configured facts for application questions (Phase 3.1).

Only values the user wrote into .env (APPLICANT_*) are ever used as answers. Empty means
"not configured", and such a question goes to the human. Nothing here guesses, derives or
converts a value: validation only rejects values that cannot be what the question asks for.
Never log the values themselves.
"""

import os
import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping, Optional

from config import MASTER_RESUME_PATH

FACT_FIELDS = (
    "total_experience",
    "current_ctc",
    "expected_ctc",
    "notice_period",
    "current_location",
    "preferred_location",
    "phone",
    "email",
)
PREFERENCE_FIELDS = ("willing_to_relocate", "work_from_office", "work_night_shift", "work_weekends")

_NUMBER = r"\d+(?:\.\d+)?"
_VALIDATORS = {
    "total_experience": re.compile(rf"^{_NUMBER}(\s*(years?|yrs?))?$", re.I),
    "current_ctc": re.compile(rf"^{_NUMBER}(\s*(lpa|lakhs?|lacs?|l|lakhs? per annum|lacs? per annum))?$", re.I),
    "expected_ctc": re.compile(rf"^{_NUMBER}(\s*(lpa|lakhs?|lacs?|l|lakhs? per annum|lacs? per annum))?$", re.I),
    "notice_period": re.compile(r"^(immediate(ly)?|immediate joiner|\d+(\s*(days?|weeks?|months?))?|serving notice.{0,40})$", re.I),
    "phone": re.compile(r"^\+?[\d\s-]{10,16}$"),
    "email": re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"),
    "current_location": re.compile(r"^[^\n]{2,100}$"),
    "preferred_location": re.compile(r"^[^\n]{2,100}$"),
}
_MAX_EXPERIENCE_YEARS = 60
_TRUE, _FALSE = ("true", "yes", "y", "1"), ("false", "no", "n", "0")


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


@dataclass(frozen=True)
class ApplicationProfile:
    total_experience: Optional[str] = None
    current_ctc: Optional[str] = None
    expected_ctc: Optional[str] = None
    notice_period: Optional[str] = None
    current_location: Optional[str] = None
    preferred_location: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    willing_to_relocate: Optional[str] = None
    work_from_office: Optional[str] = None
    work_night_shift: Optional[str] = None
    work_weekends: Optional[str] = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, Optional[str]]) -> "ApplicationProfile":
        return cls(**{f.name: _clean(values.get(f.name)) for f in fields(cls)})

    @classmethod
    def from_environment(cls, environ: Optional[Mapping[str, str]] = None) -> "ApplicationProfile":
        environ = os.environ if environ is None else environ
        return cls(**{f.name: _clean(environ.get(f"APPLICANT_{f.name.upper()}")) for f in fields(cls)})

    def configured(self) -> list[str]:
        """Names (never values) of configured fields, for diagnostics."""
        return [f.name for f in fields(self) if getattr(self, f.name)]

    def answer_for(self, name: str) -> tuple[Optional[str], str]:
        """(validated answer, "") or (None, reason). Booleans become "Yes"/"No"."""
        raw = getattr(self, name, None)
        if raw is None:
            return None, f"APPLICANT_{name.upper()} is not configured"
        if name in PREFERENCE_FIELDS:
            lowered = raw.lower()
            if lowered in _TRUE:
                return "Yes", ""
            if lowered in _FALSE:
                return "No", ""
            return None, f"APPLICANT_{name.upper()} must be true or false"
        validator = _VALIDATORS.get(name)
        if validator is not None and not validator.match(raw):
            return None, f"APPLICANT_{name.upper()} does not look like a valid {name.replace('_', ' ')}"
        if name == "total_experience" and float(re.match(_NUMBER, raw).group()) > _MAX_EXPERIENCE_YEARS:
            return None, "APPLICANT_TOTAL_EXPERIENCE is out of range"
        return raw, ""


class ResumeEvidence:
    """Skills the master resume (the AI Agent's base_resume.md) states. Read-only.

    Used only to answer "do you have experience with X?" with Yes when X is in the master
    resume. Absence is never turned into No, and no durations are derived.
    """

    def __init__(self, text: str = ""):
        self._text = " " + re.sub(r"\s+", " ", text.lower()) + " "

    @classmethod
    def from_file(cls, path: Optional[Path] = MASTER_RESUME_PATH) -> "ResumeEvidence":
        try:
            return cls(Path(path).read_text(encoding="utf-8")) if path else cls()
        except OSError:
            return cls()

    @property
    def available(self) -> bool:
        return bool(self._text.strip())

    def mentions(self, term: str) -> bool:
        term = re.sub(r"\s+", " ", term.strip().lower())
        if len(term) < 2:
            return False
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", self._text) is not None


__all__ = ["ApplicationProfile", "FACT_FIELDS", "PREFERENCE_FIELDS", "ResumeEvidence"]
