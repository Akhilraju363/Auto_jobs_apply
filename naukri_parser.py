"""Browser-independent parsing and normalization for Naukri job data.

Everything here works on plain strings/dicts so it can be unit tested without
Playwright and reused by any caller that obtains raw Naukri data.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

NAUKRI_HOME = "https://www.naukri.com"
SOURCE = "naukri"

_JOB_ID_IN_PATH = re.compile(r"-(\d{9,})/?$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass
class NaukriJob:
    """Normalized job record produced by the Phase 1 scanner."""

    title: str
    url: str
    job_id: Optional[str] = None
    company: str = ""
    location: str = ""
    experience_min: Optional[int] = None
    experience_max: Optional[int] = None
    experience_text: str = ""
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_text: str = ""
    employment_type: str = ""
    job_description: str = ""
    skills: list[str] = field(default_factory=list)
    posted_date: Optional[str] = None
    posted_label: str = ""
    posted_at: Optional[str] = None
    updated_at: Optional[str] = None
    freshness_status: str = "UNKNOWN"
    freshness_checked_at: Optional[str] = None
    source: str = SOURCE
    apply_url: Optional[str] = None
    is_easy_apply: bool = False
    is_already_applied: bool = False
    has_apply_button: bool = False
    search_keyword: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    status: str = "discovered"

    @property
    def dedup_key(self) -> str:
        return make_dedup_key(self.job_id, self.url, self.title, self.company, self.location)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["dedup_key"] = self.dedup_key
        return data


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    """Collapse runs of spaces per line but keep paragraph/line structure."""
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def clean_inline(value: Any) -> str:
    """Single-line variant of clean_text for short fields (title, company...)."""
    return re.sub(r"\s+", " ", clean_text(value)).strip()


class _HTMLTextExtractor(HTMLParser):
    _BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n- ")
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    return clean_text("".join(extractor.parts))


def normalize_location(value: Any) -> str:
    """Accept a string or list of places; return a de-duplicated, comma-joined string."""
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [clean_inline(v) for v in value]
    else:
        parts = [clean_inline(p) for p in re.split(r",(?![^()]*\))", str(value))]
    seen: list[str] = []
    for part in parts:
        if part and part not in ("-",) and part.lower() not in (s.lower() for s in seen):
            seen.append(part)
    return ", ".join(seen)


def normalize_skills(values: Any) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        values = values.split(",")
    skills: list[str] = []
    lowered: set[str] = set()
    for value in values:
        skill = clean_inline(value)
        if skill and skill.lower() not in lowered:
            lowered.add(skill.lower())
            skills.append(skill)
    return skills


# ---------------------------------------------------------------------------
# URL / identity helpers
# ---------------------------------------------------------------------------


def normalize_job_url(url: Optional[str]) -> Optional[str]:
    """Absolute https URL without query string, fragment or trailing slash."""
    if not url or not str(url).strip():
        return None
    url = str(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    absolute = urljoin(NAUKRI_HOME + "/", url)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def extract_job_id(url: Optional[str], explicit_id: Optional[str] = None) -> Optional[str]:
    if explicit_id and str(explicit_id).strip().isdigit():
        return str(explicit_id).strip()
    if not url:
        return None
    parts = urlsplit(url)
    match = re.search(r"(?:^|&)jobId=(\d+)", parts.query)
    if match:
        return match.group(1)
    match = _JOB_ID_IN_PATH.search(parts.path)
    return match.group(1) if match else None


def make_dedup_key(
    job_id: Optional[str], url: Optional[str], title: str = "", company: str = "", location: str = ""
) -> str:
    """Strongest available identifier: job ID, then canonical URL, then title+company+location."""
    if job_id:
        return f"{SOURCE}:id:{job_id}"
    canonical = normalize_job_url(url)
    if canonical:
        return f"{SOURCE}:url:{canonical}"
    fingerprint = "|".join(clean_inline(v).lower() for v in (title, company, location))
    return f"{SOURCE}:tcl:{fingerprint}"


def build_search_url(
    keyword: str, location: Optional[str] = None, experience: Optional[int] = None, page: int = 1
) -> str:
    """Naukri SEO search URL, e.g. /java-developer-jobs-in-pune-2?k=Java+Developer&experience=3.

    Location goes only in the slug: adding an ``l=`` parameter that doesn't match
    Naukri's canonical city slug makes it redirect and drop the page number and
    the experience filter.
    """
    slug = _slugify(keyword) + "-jobs"
    if location:
        first_location = location.split(",")[0]
        slug += "-in-" + _slugify(first_location)
    if page > 1:
        slug += f"-{page}"
    params = [f"k={quote_plus(keyword.strip())}"]
    if experience is not None:
        params.append(f"experience={int(experience)}")
    return f"{NAUKRI_HOME}/{slug}?{'&'.join(params)}"


def next_page_url(next_href: Optional[str], search_url: str) -> Optional[str]:
    """Follow the site's own Next link (canonical slug) while keeping our search filters."""
    if not next_href:
        return None
    next_parts = urlsplit(urljoin(NAUKRI_HOME + "/", next_href))
    if not next_parts.netloc.endswith("naukri.com"):
        return None
    query = urlsplit(search_url).query
    return urlunsplit(("https", next_parts.netloc, next_parts.path, query, ""))


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---------------------------------------------------------------------------
# Field parsers
# ---------------------------------------------------------------------------


def parse_experience(text: Any) -> tuple[Optional[int], Optional[int]]:
    """'2-5 Yrs' -> (2, 5); '10+ years' -> (10, None); 'Fresher' -> (0, 0)."""
    if not text:
        return None, None
    lowered = str(text).lower()
    numbers = [int(float(n)) for n in _NUMBER.findall(lowered)]
    if not numbers:
        return (0, 0) if "fresher" in lowered else (None, None)
    if len(numbers) >= 2:
        low, high = sorted(numbers[:2])
        return low, high
    if "+" in lowered:
        return numbers[0], None
    return numbers[0], numbers[0]


def experience_from_months(months: Any) -> Optional[int]:
    try:
        return int(float(months)) // 12
    except (TypeError, ValueError):
        return None


_SALARY_UNITS = (
    (re.compile(r"\b(crores?|cr)\b"), 10_000_000),
    (re.compile(r"\b(lakhs?|lacs?|lpa|l)\b"), 100_000),
    (re.compile(r"\d\s*k\b"), 1_000),
)
_MONTHLY = re.compile(r"(per month|monthly|/\s*month|\bp\.?\s?m\.?(?![a-z]))")


def parse_salary(text: Any) -> tuple[Optional[int], Optional[int]]:
    """Annual INR range from Naukri salary text; (None, None) when not disclosed."""
    if not text:
        return None, None
    lowered = str(text).lower().replace(",", "")
    if "not disclosed" in lowered:
        return None, None
    numbers = [float(n) for n in _NUMBER.findall(lowered)]
    if not numbers:
        return None, None
    multiplier = 1
    for pattern, unit in _SALARY_UNITS:
        if pattern.search(lowered):
            multiplier = unit
            break
    if _MONTHLY.search(lowered):
        multiplier *= 12
    values = [int(round(n * multiplier)) for n in numbers[:2]]
    if len(values) == 2:
        low, high = sorted(values)
        return low, high
    if re.search(r"\b(up ?to|upto|max)\b", lowered):
        return None, values[0]
    return values[0], values[0]


_RELATIVE_DAYS = re.compile(r"(\d+)\s*days?\s*ago")


def parse_posted_date(text: Any, today: Optional[date] = None) -> Optional[str]:
    """ISO date when the label is precise enough; None for vague labels like '30+ days ago'."""
    if not text:
        return None
    raw = str(text).strip()
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        pass
    today = today or date.today()
    lowered = raw.lower()
    if "+" in lowered:
        return None
    if any(word in lowered for word in ("just now", "today", "hour", "minute", "few")):
        return today.isoformat()
    match = _RELATIVE_DAYS.search(lowered)
    if match:
        return (today - timedelta(days=int(match.group(1)))).isoformat()
    return None


# ---------------------------------------------------------------------------
# Page state detection
# ---------------------------------------------------------------------------

_BLOCK_MARKERS = (
    "access denied",
    "verify you are human",
    "are you a robot",
    "unusual traffic",
    "captcha",
)


def detect_block(title: str = "", url: str = "", body: str = "") -> Optional[str]:
    """Return a reason string if the page looks like an anti-bot/CAPTCHA block."""
    haystacks = (str(title).lower(), str(url).lower(), str(body)[:2000].lower())
    for marker in _BLOCK_MARKERS:
        if any(marker in h for h in haystacks):
            return marker
    return None


def is_login_page(url: str) -> bool:
    lowered = (url or "").lower()
    return "nlogin" in lowered or "login.naukri" in lowered or "/login" in lowered


# ---------------------------------------------------------------------------
# Raw page data -> NaukriJob
# ---------------------------------------------------------------------------


def parse_card(raw: Any, search_keyword: str = "") -> Optional[NaukriJob]:
    """Build a job from raw search-card data; None if the card is unusable."""
    if not isinstance(raw, dict):
        return None
    url = normalize_job_url(raw.get("url"))
    title = clean_inline(raw.get("title"))
    if not url or not title:
        return None
    experience_text = clean_inline(raw.get("experience"))
    salary_text = clean_inline(raw.get("salary"))
    posted_label = clean_inline(raw.get("posted"))
    exp_min, exp_max = parse_experience(experience_text)
    sal_min, sal_max = parse_salary(salary_text)
    return NaukriJob(
        title=title,
        url=url,
        job_id=extract_job_id(url, raw.get("job_id")),
        company=clean_inline(raw.get("company")),
        location=normalize_location(raw.get("location")),
        experience_min=exp_min,
        experience_max=exp_max,
        experience_text=experience_text,
        salary_min=sal_min,
        salary_max=sal_max,
        salary_text=salary_text,
        skills=normalize_skills(raw.get("tags")),
        posted_date=parse_posted_date(posted_label),
        posted_label=posted_label,
        search_keyword=search_keyword,
    )


def parse_cards(raw_cards: list, search_keyword: str = "") -> tuple[list[NaukriJob], int]:
    """Parse a page of cards; malformed cards are logged and counted, never raised."""
    jobs: list[NaukriJob] = []
    malformed = 0
    for index, raw in enumerate(raw_cards or []):
        try:
            job = parse_card(raw, search_keyword)
        except Exception as e:  # defensive: one bad card must not stop the page
            logger.warning(f"Job card {index} extraction failed: {e}")
            job = None
        if job is None:
            malformed += 1
            logger.warning(f"Skipping malformed job card {index} (missing title or URL)")
            continue
        jobs.append(job)
    return jobs, malformed


def dedupe_jobs(jobs: list[NaukriJob]) -> list[NaukriJob]:
    seen: set[str] = set()
    unique: list[NaukriJob] = []
    for job in jobs:
        if job.dedup_key not in seen:
            seen.add(job.dedup_key)
            unique.append(job)
    return unique


def find_job_posting(ld_blocks: list) -> dict:
    """Pick the schema.org JobPosting object from parsed JSON-LD blocks."""
    candidates: list = []
    for block in ld_blocks or []:
        if isinstance(block, str):
            try:
                block = json.loads(block)
            except ValueError:
                continue
        if isinstance(block, dict) and "@graph" in block:
            candidates.extend(block["@graph"])
        elif isinstance(block, list):
            candidates.extend(block)
        else:
            candidates.append(block)
    for item in candidates:
        if isinstance(item, dict) and item.get("@type") == "JobPosting":
            return item
    return {}


def classify_apply_buttons(buttons: list) -> dict:
    """Interpret apply-related buttons found on a job detail page (read only, never clicked)."""
    state = {"has_apply_button": False, "is_easy_apply": False, "is_already_applied": False, "apply_url": None}
    for button in buttons or []:
        if not isinstance(button, dict):
            continue
        element_id = str(button.get("id") or "").lower()
        text = clean_inline(button.get("text")).lower()
        if element_id == "already-applied" or text == "applied":
            state["is_already_applied"] = True
        elif element_id == "company-site-button" or "company site" in text:
            state["has_apply_button"] = True
            href = normalize_job_url(button.get("href")) if button.get("href") else None
            state["apply_url"] = state["apply_url"] or href
        elif element_id == "apply-button" or text in ("apply", "apply now"):
            state["has_apply_button"] = True
            state["is_easy_apply"] = True
    return state


def _ld_salary_text(posting: dict) -> str:
    value = (posting.get("baseSalary") or {}).get("value") if isinstance(posting.get("baseSalary"), dict) else None
    if not isinstance(value, dict):
        return ""
    if value.get("value"):
        return clean_inline(value["value"])
    low, high = value.get("minValue"), value.get("maxValue")
    if low or high:
        return clean_inline(f"{low or ''}-{high or ''}".strip("-"))
    return ""


def _ld_location(posting: dict) -> Any:
    locations = posting.get("jobLocation")
    if isinstance(locations, dict):
        locations = [locations]
    places: list[str] = []
    for place in locations or []:
        address = place.get("address", {}) if isinstance(place, dict) else {}
        locality = address.get("addressLocality") if isinstance(address, dict) else None
        if isinstance(locality, list):
            places.extend(locality)
        elif locality:
            places.append(locality)
    return places


def merge_detail(job: NaukriJob, detail: dict, today: Optional[date] = None) -> NaukriJob:
    """Overlay data from a job detail page onto a card-level job.

    DOM text is preferred (it is what the user sees); JSON-LD fills any gaps.
    """
    detail = detail or {}
    posting = find_job_posting(detail.get("ld_json", []))
    details = {clean_inline(k): clean_inline(v) for k, v in (detail.get("details") or {}).items()}

    job.title = clean_inline(detail.get("title")) or clean_inline(posting.get("title")) or job.title
    org = posting.get("hiringOrganization") if isinstance(posting.get("hiringOrganization"), dict) else {}
    job.company = clean_inline(detail.get("company")) or clean_inline(org.get("name")) or job.company
    job.location = (
        normalize_location(detail.get("location")) or normalize_location(_ld_location(posting)) or job.location
    )

    identifier = posting.get("identifier") if isinstance(posting.get("identifier"), dict) else {}
    job.job_id = job.job_id or extract_job_id(job.url, identifier.get("value"))

    experience_text = clean_inline(detail.get("experience"))
    if experience_text:
        job.experience_text = experience_text
        job.experience_min, job.experience_max = parse_experience(experience_text)
    elif job.experience_min is None:
        months = (posting.get("experienceRequirements") or {}) if isinstance(posting.get("experienceRequirements"), dict) else {}
        job.experience_min = experience_from_months(months.get("monthsOfExperience"))

    salary_text = clean_inline(detail.get("salary")) or _ld_salary_text(posting)
    if salary_text:
        job.salary_text = salary_text
        job.salary_min, job.salary_max = parse_salary(salary_text)

    description = clean_text(detail.get("description")) or html_to_text(posting.get("description", ""))
    job.job_description = description or job.job_description

    skills = normalize_skills(detail.get("skills")) or normalize_skills(posting.get("skills"))
    job.skills = skills or job.skills

    job.employment_type = (
        details.get("Employment Type") or clean_inline(posting.get("employmentType")) or job.employment_type
    )

    posted_iso = parse_posted_date(posting.get("datePosted"), today)
    posted_label = details.get("Posted") or job.posted_label
    job.posted_label = posted_label
    job.posted_date = posted_iso or parse_posted_date(posted_label, today) or job.posted_date

    apply_state = classify_apply_buttons(detail.get("buttons"))
    job.has_apply_button = apply_state["has_apply_button"]
    job.is_easy_apply = apply_state["is_easy_apply"]
    job.is_already_applied = job.is_already_applied or apply_state["is_already_applied"]
    job.apply_url = apply_state["apply_url"] or (job.url if job.is_easy_apply else None)

    metadata_keys = {
        "Role": "role",
        "Industry Type": "industry",
        "Department": "department",
        "Role Category": "role_category",
        "Education": "education",
        "UG": "education_ug",
        "PG": "education_pg",
        "Openings": "openings",
        "Applicants": "applicants",
    }
    for label, key in metadata_keys.items():
        if details.get(label):
            job.metadata[key] = details[label]
    if detail.get("work_mode"):
        job.metadata["work_mode"] = clean_inline(detail["work_mode"])
    return job


__all__ = [
    "NaukriJob",
    "build_search_url",
    "classify_apply_buttons",
    "clean_text",
    "dedupe_jobs",
    "detect_block",
    "extract_job_id",
    "is_login_page",
    "make_dedup_key",
    "merge_detail",
    "next_page_url",
    "normalize_job_url",
    "parse_card",
    "parse_cards",
    "parse_experience",
    "parse_posted_date",
    "parse_salary",
]
