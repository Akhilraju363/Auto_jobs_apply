"""Deterministic classification and answering of application questions (Phase 3.1).

    question text -> category -> configured fact? -> validated answer, else the human

No LLM, no guessing. Only explicitly configured applicant facts (ApplicationProfile) are used,
plus "Yes" to "do you have experience with X?" when X is in the master resume. Free text,
legal, sensitive, unknown and unconfigured questions always go to the human. Rules are ordered:
sensitive and free-text checks run before any answerable category, so a question that mentions
both (e.g. "Are you willing to relocate? Please explain why") goes to the human.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from application_profile import ApplicationProfile, ResumeEvidence


class QuestionCategory(str, Enum):
    EXPECTED_CTC = "expected_ctc"
    CURRENT_CTC = "current_ctc"
    TOTAL_EXPERIENCE = "total_experience"
    NOTICE_PERIOD = "notice_period"
    CURRENT_LOCATION = "current_location"
    PREFERRED_LOCATION = "preferred_location"
    PHONE = "phone"
    EMAIL = "email"
    WILLING_TO_RELOCATE = "willing_to_relocate"
    WORK_FROM_OFFICE = "work_from_office"
    WORK_NIGHT_SHIFT = "work_night_shift"
    WORK_WEEKENDS = "work_weekends"
    TECHNICAL_SKILL = "technical_skill"
    EDUCATION = "education"
    EMPLOYMENT_HISTORY = "employment_history"
    FREE_TEXT = "free_text"
    LEGAL_DECLARATION = "legal_declaration"
    WORK_AUTHORIZATION = "work_authorization"
    DEMOGRAPHIC = "demographic"
    IDENTITY = "identity"
    UNKNOWN = "unknown"


C = QuestionCategory
FACTUAL = (C.EXPECTED_CTC, C.CURRENT_CTC, C.TOTAL_EXPERIENCE, C.NOTICE_PERIOD, C.CURRENT_LOCATION,
           C.PREFERRED_LOCATION, C.PHONE, C.EMAIL)
PREFERENCES = (C.WILLING_TO_RELOCATE, C.WORK_FROM_OFFICE, C.WORK_NIGHT_SHIFT, C.WORK_WEEKENDS)
MANUAL_ONLY = (C.FREE_TEXT, C.LEGAL_DECLARATION, C.WORK_AUTHORIZATION, C.DEMOGRAPHIC, C.IDENTITY,
               C.EDUCATION, C.EMPLOYMENT_HISTORY, C.UNKNOWN)

# Outcome codes (also stored as jobs.application_code by the runner).
QUESTION_AUTO_ANSWERED = "QUESTION_AUTO_ANSWERED"
QUESTION_MANUAL_REQUIRED = "QUESTION_MANUAL_REQUIRED"
UNCONFIGURED_ANSWER = "UNCONFIGURED_ANSWER"
UNSUPPORTED_QUESTION = "UNSUPPORTED_QUESTION"

_COMPENSATION = r"(ctc|salary|compensation|package|pay|remuneration)"
_RULES: tuple[tuple[QuestionCategory, re.Pattern], ...] = tuple(
    (category, re.compile(pattern)) for category, pattern in (
        # --- always human: identity, legal, authorization, demographic, free text -------------
        (C.IDENTITY, r"\b(aadha?ar|pan( card| number| no)?|passport( number| no)?|ssn|national id|id proof|"
                     r"identity (card|proof|number)|uan)\b"),
        (C.LEGAL_DECLARATION, r"\b(declar\w*|consent|i agree|agree to|terms|criminal|convict\w*|background (check|verification)|"
                              r"conflict of interest|non[- ]?compete|bond|contractual|restriction|certify|attest|"
                              r"legal (action|proceeding)|terminated|dismissed)\b"),
        (C.WORK_AUTHORIZATION, r"\b(authori[sz]\w*|visa|sponsor\w*|citizen\w*|work permit|eligible to work|"
                               r"right to work|nationality|green card|h-?1b)\b"),
        (C.DEMOGRAPHIC, r"\b(gender|sex|age|date of birth|dob|born|race|ethnic\w*|religio\w*|caste|"
                        r"disab\w*|veteran|marital|pronoun\w*)\b"),
        (C.FREE_TEXT, r"\b(why|tell us|describe|explain|elaborate|about yourself|career goals?|"
                      r"looking for (a )?change|reason (for|to)|motivat\w*|cover letter|additional (info\w*|details|comments)|"
                      r"what makes you|summar\w*|strengths?|weakness\w*|achievements?)\b"),
        # --- explicit personal preferences (configured booleans only) --------------------------
        (C.WILLING_TO_RELOCATE, r"\b(relocat\w*|shift(ing)? to|move to)\b"),
        (C.WORK_NIGHT_SHIFT, r"\b(night shifts?|rotational shifts?|(us|uk) shifts?|shift timings?|night)\b"),
        (C.WORK_WEEKENDS, r"\b(weekends?|saturdays?|sundays?)\b"),
        (C.WORK_FROM_OFFICE, r"\b(work from office|wfo|from (the )?office|in[- ]office|on[- ]?site|"
                             r"hybrid|office (daily|all days|5 days)|5 days (from|in) (the )?office|come to (the )?office)\b"),
        # --- facts ------------------------------------------------------------------------------
        (C.EXPECTED_CTC, rf"\b(expected {_COMPENSATION}|{_COMPENSATION} expectations?|ectc|expecting|"
                         rf"desired {_COMPENSATION}|{_COMPENSATION} are you (expecting|looking for))\b"),
        (C.CURRENT_CTC, rf"\b(current {_COMPENSATION}|present {_COMPENSATION}|cctc|currently earning|"
                        rf"last drawn|existing {_COMPENSATION}|{_COMPENSATION} (now|currently))\b"),
        (C.NOTICE_PERIOD, r"\b(notice period|notice|how soon can you join|when can you (join|start)|"
                          r"joining (time|date)|earliest (joining|start)|available to (join|start)|"
                          r"days to join|join (immediately|within))\b"),
        (C.PREFERRED_LOCATION, r"\b(preferred (job |work )?(location|city)|location preference|"
                               r"preferred place|where would you (like|prefer) to work)\b"),
        (C.CURRENT_LOCATION, r"\b(current(ly)? (location|city|residence)|where are you (currently )?"
                             r"(located|based|staying|living|residing)|which city (are you|do you) (in|live|stay))\b"),
        (C.PHONE, r"\b(phone|mobile|contact) (number|no)\b|\bphone\b|\bmobile\b"),
        (C.EMAIL, r"\be-?mail\b"),
        # --- needs the resume or the human ----------------------------------------------------
        (C.EDUCATION, r"\b(degree|graduat\w*|education\w*|qualification\w*|cgpa|gpa|percentage|university|"
                      r"college|b\.?\s?tech|b\.?e\b|mca|bca|m\.?\s?tech|diploma|passing year|year of passing)\b"),
        (C.EMPLOYMENT_HISTORY, r"\b(current (company|employer|organi[sz]ation|designation|role)|previous "
                               r"(company|employer)|last (company|employer)|employer|reporting manager|"
                               r"companies? (have you )?worked)\b"),
    )
)

_SKILL_YES_NO = re.compile(
    r"^(do|did|have|has|are|is) you (have )?(any |hands[- ]on |prior |professional |working |practical )?"
    r"(experience|exposure|knowledge|worked|hands[- ]on)\s*(with|in|on|of|using)?\s+(?P<skill>.+?)\??$"
)
_EXPERIENCE_WORDS = re.compile(r"\b(experience|exp|years?|yrs?|worked|knowledge|exposure|hands[- ]on|"
                               r"proficien\w*|familiar\w*|rate|rating|skill\w*|expertise)\b")
_SKILL_QUALIFIER = re.compile(r"\b(in|with|on|using|of)\s+[a-z0-9.#+/ -]{2,}")
_DURATION_OR_LEVEL = re.compile(r"\b(how many|how much|years?|yrs?|months?|rate|rating|level|scale|out of|proficien\w*)\b")
_TOTAL_EXPERIENCE = re.compile(r"\b(total|overall|relevant work|professional|work|it)\b.*\bexperience\b|"
                               r"^(how many )?years of experience( do you have)?$|^experience( in years)?$|"
                               r"^total (exp|experience)$")


def normalize_question(text: str) -> str:
    q = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return re.sub(r"[\s*:?.!]+$", "", q).strip()


def classify_question(text: str) -> QuestionCategory:
    q = normalize_question(text)
    if not q:
        return C.UNKNOWN
    for category, pattern in _RULES[:5]:  # always-human groups first
        if pattern.search(q):
            return category
    # Experience questions: "in/with <skill>" is a technical question, otherwise total experience.
    if _EXPERIENCE_WORDS.search(q) and not re.search(rf"\b{_COMPENSATION}\b|notice", q):
        if _SKILL_YES_NO.match(q.rstrip("?")) or (_SKILL_QUALIFIER.search(q) and not _TOTAL_EXPERIENCE.search(q)):
            return C.TECHNICAL_SKILL
        if _TOTAL_EXPERIENCE.search(q):
            return C.TOTAL_EXPERIENCE
        if re.search(r"\b(rate|rating|proficien\w*|familiar\w*|skill\w*|expertise)\b", q):
            return C.TECHNICAL_SKILL
    for category, pattern in _RULES[5:]:
        if pattern.search(q):
            return category
    return C.UNKNOWN


@dataclass
class AnswerDecision:
    category: QuestionCategory
    value: Optional[str] = None  # never log this
    code: str = QUESTION_MANUAL_REQUIRED
    reason: str = ""
    source: str = ""

    @property
    def automatic(self) -> bool:
        return self.value is not None


def _skill_terms(phrase: str) -> list[str]:
    phrase = re.sub(r"\b(technologies|technology|framework|frameworks|tools?|platforms?|stack)\b", " ", phrase)
    parts = re.split(r",|/|\band\b|&", phrase)
    return [p.strip(" .?") for p in parts if p.strip(" .?")]


def _technical_answer(q: str, evidence: Optional[ResumeEvidence]) -> AnswerDecision:
    decision = AnswerDecision(C.TECHNICAL_SKILL, reason="technical question needs you")
    if _DURATION_OR_LEVEL.search(q):
        decision.reason = "asks for a duration or rating, which is never derived"
        return decision
    match = _SKILL_YES_NO.match(q.rstrip("?"))
    if not match or re.search(r"\bor\b", match.group("skill")):
        return decision
    terms = _skill_terms(match.group("skill"))
    if not terms or evidence is None or not evidence.available:
        decision.reason = "no master resume evidence available"
        return decision
    if all(evidence.mentions(term) for term in terms):
        return AnswerDecision(C.TECHNICAL_SKILL, "Yes", QUESTION_AUTO_ANSWERED,
                              "skill is stated in the master resume", "master_resume")
    decision.reason = "skill not found in the master resume (never answered No automatically)"
    return decision


def _match_option(value: str, options: list[str]) -> Optional[str]:
    return next((o for o in options if o.strip().lower() == value.strip().lower()), None)


def decide_answer(
    question: str,
    profile: ApplicationProfile,
    evidence: Optional[ResumeEvidence] = None,
    input_kind: str = "text",
    options: Optional[list[str]] = None,
) -> AnswerDecision:
    """What to answer, or why the human must. value is set only for safe, configured answers."""
    category = classify_question(question)
    if input_kind in ("textarea", "file", "checkbox"):
        return AnswerDecision(category, code=UNSUPPORTED_QUESTION, reason=f"{input_kind} answers are never automated")
    if category in MANUAL_ONLY:
        return AnswerDecision(category, code=QUESTION_MANUAL_REQUIRED,
                              reason=f"{category.value.replace('_', ' ')} questions are answered by you")
    if category == C.TECHNICAL_SKILL:
        decision = _technical_answer(normalize_question(question), evidence)
    else:
        value, why = profile.answer_for(category.value)
        if value is None:
            return AnswerDecision(category, code=UNCONFIGURED_ANSWER, reason=why)
        decision = AnswerDecision(category, value, QUESTION_AUTO_ANSWERED, "configured applicant fact", "application_profile")
    if decision.automatic and options:
        choice = _match_option(decision.value, options)
        if choice is None:
            return AnswerDecision(category, code=UNCONFIGURED_ANSWER,
                                  reason="the configured answer matches none of the offered options")
        decision.value = choice
    return decision


__all__ = [
    "AnswerDecision",
    "FACTUAL",
    "MANUAL_ONLY",
    "PREFERENCES",
    "QUESTION_AUTO_ANSWERED",
    "QUESTION_MANUAL_REQUIRED",
    "QuestionCategory",
    "UNCONFIGURED_ANSWER",
    "UNSUPPORTED_QUESTION",
    "classify_question",
    "decide_answer",
    "normalize_question",
]
