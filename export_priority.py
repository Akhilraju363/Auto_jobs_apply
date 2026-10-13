"""Relevance-first order for the AI Agent export (the per-run cap is small, so the order decides).

Deterministic, offline, no LLM. Only the ORDER of jobs that already passed every gate (24-hour
freshness, 4+ years experience, valid record) changes; nothing here makes a job eligible or not.

Rank, best first:
  1. role tier from the title, for the target roles
       4  Java + Spring / Spring Boot / Backend / Full Stack   (Java Spring Boot, Java Backend, Java Full Stack)
       3  Java developer / engineer / programmer               (Java Developer)
       2  Full Stack developer / engineer, no other stack named (Full Stack Developer)
       1  generic software developer / engineer / SDE title whose key skills or description mention Java
       0  anything else -- testing/QA titles and titles naming another stack (.NET, Python, PHP, ...) too
  2. matching skills: distinct target skills in the title, key skills and description
  3. expiry risk: the older posting first (it leaves the 24-hour window sooner); unknown age last
  4. jobs.id, ascending (stable tie-break)
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from eligibility import UNKNOWN, job_freshness

TARGET_ROLES = ("Java Developer", "Java Spring Boot Developer", "Java Backend Developer",
                "Java Full Stack Developer", "Full Stack Developer")

_I = re.IGNORECASE
_JAVA = re.compile(r"\bjava\b(?!\s*script)", _I)  # Java, JAVA, Java/J2EE -- never JavaScript
_SPECIALIZATION = re.compile(r"\bspring(?:\s*boot)?\b|\bspringboot\b|\bback[\s-]?end\b|\bfull[\s-]?stack\b", _I)
_FULL_STACK = re.compile(r"\bfull[\s-]?stack\b", _I)
_DEV_ROLE = re.compile(r"\b(?:developer|engineer|programmer|sde)\b", _I)
# A title that is about testing / QA is not a target developer role, even when it mentions Java.
_TESTING = re.compile(r"\b(?:test|tester|testing|qa|sdet|quality)\b", _I)
# A title naming another primary stack is not a Java / Full Stack target (unless it also names Java).
_OTHER_STACK = re.compile(r"\.net\b|\bdot\s*net\b|\bc#|\bpython\b|\bphp\b|\bflutter\b|\breact\s*native\b|"
                          r"\bnode(?:\.?js)?\b|\bruby\b|\bgolang\b|\bmendix\b|\bservice\s*now\b|\bsalesforce\b|"
                          r"\bsap\b|\bunreal\b|\bui\s*path\b|\bt24\b", _I)
# Target skills (distinct names counted once each).
TARGET_SKILLS = {
    "Java": r"\bjava\b(?!\s*script)",
    "Spring Boot": r"\bspring\s*boot\b|\bspringboot\b",
    "Spring": r"\bspring\b",
    "Microservices": r"\bmicro[\s-]?services?\b",
    "REST": r"\brest(?:ful)?\b",
    "Hibernate": r"\bhibernate\b",
    "JPA": r"\bjpa\b",
    "J2EE": r"\bj2ee\b|\bjee\b",
    "Angular": r"\bangular(?:\.?js)?\b",
    "React": r"\breact(?:\.?js)?\b(?!\s*native)",
    "SQL": r"\b(?:sql|mysql|postgres(?:ql)?|oracle)\b",
    "Kafka": r"\bkafka\b",
    "JUnit": r"\bjunit\b",
}
_SKILL_PATTERNS = {name: re.compile(pattern, _I) for name, pattern in TARGET_SKILLS.items()}
TIER_LABELS = {4: "Java specialization", 3: "Java developer", 2: "Full Stack", 1: "Java in skills", 0: "other role"}


@dataclass(frozen=True)
class Priority:
    tier: int
    skills: tuple  # matching target skill names, in TARGET_SKILLS order
    age_hours: Optional[float]

    def describe(self) -> str:
        age = f"{self.age_hours:.1f}h old" if self.age_hours is not None else "age unknown"
        return f"{TIER_LABELS[self.tier]}, {len(self.skills)} matching skill(s), {age}"


def _skills_text(job: dict) -> str:
    skills = job.get("skills") or []
    return " ".join(str(s) for s in skills) if isinstance(skills, (list, tuple)) else str(skills)


def role_tier(job: dict) -> int:
    title = str(job.get("title") or "")
    if _TESTING.search(title):
        return 0
    java_title = bool(_JAVA.search(title))
    if java_title and _SPECIALIZATION.search(title):
        return 4
    if java_title:
        return 3
    if _OTHER_STACK.search(title):
        return 0
    if _FULL_STACK.search(title):
        return 2
    if _DEV_ROLE.search(title) and _JAVA.search(f"{_skills_text(job)} {job.get('job_description') or ''}"):
        return 1
    return 0


def matching_skills(job: dict) -> tuple:
    text = f"{job.get('title') or ''} {_skills_text(job)} {job.get('job_description') or ''}"
    return tuple(name for name, pattern in _SKILL_PATTERNS.items() if pattern.search(text))


def priority(job: dict, reference_time: datetime, window_hours: int = 24) -> Priority:
    status, age = job_freshness(job, reference_time=reference_time, window_hours=window_hours)
    return Priority(role_tier(job), matching_skills(job), None if status == UNKNOWN else age)


def relevance_order(reference_time: datetime, window_hours: int = 24) -> Callable[[dict], tuple]:
    """Sort key for export_discovered_jobs(order_key=...): best first, fully deterministic."""
    def key(job: dict) -> tuple:
        p = priority(job, reference_time, window_hours)
        expiry = (0, -p.age_hours) if p.age_hours is not None else (1, 0.0)
        return (-p.tier, -len(p.skills), expiry, job["id"])

    return key


__all__ = ["TARGET_ROLES", "TARGET_SKILLS", "Priority", "role_tier", "matching_skills", "priority",
           "relevance_order"]
