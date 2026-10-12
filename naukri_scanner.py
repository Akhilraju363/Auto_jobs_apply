"""Phase 1 Naukri job discovery: search -> cards -> job details -> normalize -> dedupe -> SQLite.

This module is the single place that knows Naukri's DOM. NaukriDriver reuses its
selectors and browser launch helper instead of keeping its own copies.
It never clicks Apply or submits anything.
"""

import logging
import random
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Callable, Optional

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Playwright, sync_playwright

from config import (
    APPLICATION_JOB_WINDOW_HOURS,
    CHROME_USER_DATA,
    NAUKRI_DELAY_MAX,
    NAUKRI_DELAY_MIN,
    NAUKRI_EXPERIENCE,
    NAUKRI_JOB_AGE_DAYS,
    NAUKRI_KEYWORDS,
    NAUKRI_LOCATION,
    NAUKRI_LOCATIONS,
    NAUKRI_MAX_JOBS,
    NAUKRI_MAX_PAGES,
    NAUKRI_MAX_SEARCH_PAGES,
    NAUKRI_SEARCH_ALL_LOCATIONS,
    PREFERRED_LOCATIONS,
)
from db import find_job_row_id, get_job, init_db, is_job_applied, job_exists, save_discovered_job
from naukri_parser import (
    NaukriJob,
    build_search_url,
    detect_block,
    is_login_page,
    merge_detail,
    next_page_url,
    parse_cards,
)
from eligibility import STALE, UNKNOWN, freshness_for, normalize_preferred_location
from experience_check import INELIGIBLE, check_experience, record_experience_check, verified_resume_experience

logger = logging.getLogger(__name__)

NAVIGATION_TIMEOUT_MS = 45_000
CONTENT_TIMEOUT_MS = 15_000

# Tried in order; the first selector that matches any element wins (avoids
# double-counting nested wrappers). Newest Naukri layout first.
CARD_SELECTORS = ("div.srp-jobtuple-wrapper", "div.cust-job-tuple", "article.jobTuple")

DETAIL_READY_SELECTOR = (
    "[class*='jd-header-title'], [class*='job-desc'], .jobDesc, script[type='application/ld+json']"
)
# The description itself. JSON-LD and the header often attach before the description is rendered,
# so an empty first read waits for this before reading again.
DESCRIPTION_SELECTOR = (
    "[class*='dang-inner-html'], [class*='job-desc-container'], section[class*='job-desc'], "
    "[class*='JDC__'], .jobDesc, .job-desc"
)
DESCRIPTION_RETRY_WAIT_SECONDS = 2.0

_FROM_MASTER_RESUME = object()  # NaukriScanner default: verified experience from MASTER_RESUME_PATH

# Runs inside the page against one card element; returns raw strings only.
CARD_EXTRACT_JS = """
el => {
  const first = sels => { for (const s of sels) { const n = el.querySelector(s); if (n) return n; } return null; };
  const text = sels => { const n = first(sels); return n ? (n.getAttribute('title') || n.innerText || '').trim() : ''; };
  const link = first(['a.title', 'a.jobTitle', 'a.jobCardLink', 'h2 a', "a[href*='job-listings']"]);
  const idHolder = el.matches('[data-job-id]') ? el : el.querySelector('[data-job-id]');
  return {
    job_id: idHolder ? idHolder.getAttribute('data-job-id') : '',
    url: link ? (link.getAttribute('href') || '') : '',
    title: link ? (link.getAttribute('title') || link.innerText || '').trim() : '',
    company: text(['a.comp-name', 'a.companyName', "[class*='comp-name']"]),
    location: text(['.locWdth', '.loc-wrap', '.location', "[class*='loc-wrap']"]),
    experience: text(['.expwdth', '.exp-wrap', '.experience']),
    salary: text(['.sal-wrap span[title]', '.sal-wrap', '.salary']),
    posted: text(['.job-post-day', "[class*='post-day']"]),
    tags: Array.from(el.querySelectorAll('ul.tags-gt li, ul.tags li')).map(n => n.innerText.trim()).filter(Boolean),
  };
}
"""

DETAIL_EXTRACT_JS = """
() => {
  const first = sels => { for (const s of sels) { const n = document.querySelector(s); if (n) return n; } return null; };
  const text = sels => { const n = first(sels); return n ? (n.innerText || '').trim() : ''; };
  const ld = [];
  document.querySelectorAll("script[type='application/ld+json']").forEach(s => {
    try { ld.push(JSON.parse(s.textContent)); } catch (e) {}
  });
  const details = {};
  document.querySelectorAll("[class*='other-details'] [class*='details'], [class*='education'] [class*='details'], [class*='jhc__stat']")
    .forEach(n => {
      const label = n.querySelector('label');
      if (!label) return;
      const key = label.innerText.replace(':', '').trim();
      const value = n.innerText.replace(label.innerText, '').replace(/,\\s*$/, '').trim();
      if (key && value) details[key] = value;
    });
  const companyNode = first(["[class*='jd-header-comp-name'] a", "[class*='jd-header-comp-name']", '.jd-header-comp-name']);
  const skillNodes = document.querySelectorAll("[class*='key-skill'] a, [class*='key-skill'] [class*='chip']");
  const buttons = Array.from(document.querySelectorAll('button, a'))
    .filter(b => /apply/i.test(b.id || '') || /^\\s*(apply|apply now|applied|apply on company site)\\s*$/i.test(b.innerText || ''))
    .map(b => ({ id: b.id || '', text: (b.innerText || '').trim(), href: b.getAttribute('href') || '' }));
  return {
    title: text(["[class*='jd-header-title']", 'h1']),
    company: companyNode ? (companyNode.innerText || '').split('\\n')[0].trim() : '',
    location: text(["[class*='jhc__location']", "[class*='jhc__loc']", '.location']),
    experience: text(["[class*='jhc__exp']", '.exp']),
    salary: text(["[class*='jhc__salary']", '.salary']),
    work_mode: text(["[class*='jhc__wfhmode']"]),
    description: text(["[class*='dang-inner-html']", "[class*='job-desc-container']", "section[class*='job-desc']",
                       "[class*='JDC__']", '.jobDesc', '.job-desc']),
    skills: Array.from(skillNodes).map(n => (n.innerText || '').trim()).filter(Boolean),
    details: details,
    buttons: buttons,
    ld_json: ld,
  };
}
"""


class ScanStopped(Exception):
    """Raised when the scan must stop: login wall, CAPTCHA or block page."""


class DescriptionMissing(ValueError):
    """The job page loaded but no description could be read, even after waiting and one reload."""


@dataclass
class ScanResult:
    keywords: list[str]
    pages_scanned: int = 0
    cards_found: int = 0
    malformed_cards: int = 0
    unique_jobs: int = 0
    new_jobs_stored: int = 0
    duplicates_skipped: int = 0
    failed_job_pages: int = 0
    searches: int = 0  # keyword/location searches started
    skipped_stale: int = 0  # search cards already older than the freshness window (no detail page opened)
    skipped_experience: int = 0  # search cards whose Naukri experience minimum exceeds the resume
    description_missing: int = 0  # detail pages without a readable description (not stored; retried next scan)
    stopped_reason: Optional[str] = None
    jobs: list[NaukriJob] = field(default_factory=list)

    def summary(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if k != "jobs"}
        data["jobs"] = len(self.jobs)
        return data


def launch_browser_context(playwright: Playwright, headless: bool) -> BrowserContext:
    """Persistent Chromium profile shared by the scanner and NaukriDriver (keeps the Naukri login)."""
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(CHROME_USER_DATA),
        headless=headless,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )


def find_card_handles(page: Page) -> list:
    for selector in CARD_SELECTORS:
        cards = page.query_selector_all(selector)
        if cards:
            return cards
    return []


class NaukriScanner:
    """Discover Naukri jobs and store them with status='discovered'.

    Pass an existing Playwright ``page`` to reuse a browser session (e.g. from
    NaukriDriver); otherwise the scanner opens the shared persistent profile.
    """

    def __init__(
        self,
        page: Optional[Page] = None,
        headless: bool = False,
        keywords: Optional[list[str]] = None,
        location: Optional[str] = None,
        experience: Optional[int] = None,
        max_pages: Optional[int] = None,
        max_jobs: Optional[int] = None,
        delay_range: Optional[tuple[float, float]] = None,
        sleep: Callable[[float], None] = time.sleep,
        locations: Optional[list[str]] = None,
        search_all_locations: Optional[bool] = None,
        job_age_days: Optional[int] = None,
        max_search_pages: Optional[int] = None,
        verified_experience=_FROM_MASTER_RESUME,
        window_hours: int = APPLICATION_JOB_WINDOW_HOURS,
    ):
        """location: one explicit search location (the pre-existing single-location mode).
        locations: searched in order; default NAUKRI_LOCATIONS, then NAUKRI_LOCATION, then PREFERRED_LOCATIONS.
        search_all_locations: finish with one location-free search per keyword (default
        NAUKRI_SEARCH_ALL_LOCATIONS; off when an explicit location is given)."""
        self.page = page
        self.headless = headless
        self.keywords = keywords or list(NAUKRI_KEYWORDS)
        self.location = location if location is not None else NAUKRI_LOCATION
        if location is not None:
            self.locations = [location] if location else []
            self.search_all_locations = not location if search_all_locations is None else search_all_locations
        else:
            configured = list(locations) if locations is not None else (
                list(NAUKRI_LOCATIONS) + [NAUKRI_LOCATION] + list(PREFERRED_LOCATIONS))
            self.locations = _search_locations(configured)
            self.search_all_locations = (NAUKRI_SEARCH_ALL_LOCATIONS if search_all_locations is None
                                         else search_all_locations)
        self.job_age_days = NAUKRI_JOB_AGE_DAYS if job_age_days is None else job_age_days
        self.max_search_pages = max_search_pages or NAUKRI_MAX_SEARCH_PAGES
        self.verified_experience = (verified_resume_experience() if verified_experience is _FROM_MASTER_RESUME
                                    else verified_experience)
        self.window_hours = window_hours
        self.experience = experience if experience is not None else NAUKRI_EXPERIENCE
        self.max_pages = max_pages or NAUKRI_MAX_PAGES
        self.max_jobs = max_jobs or NAUKRI_MAX_JOBS
        self.delay_range = delay_range or (NAUKRI_DELAY_MIN, NAUKRI_DELAY_MAX)
        self._sleep = sleep
        self._owns_browser = page is None
        self._playwright = None
        self._context = None
        self._navigations = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # -- session --------------------------------------------------------

    def start(self) -> None:
        if self.page:
            return
        try:
            self._playwright = sync_playwright().start()
            self._context = launch_browser_context(self._playwright, self.headless)
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        except Exception as e:
            logger.error(f"Browser initialization failed: {e}")
            self.close()
            raise

    def close(self) -> None:
        if not self._owns_browser:
            return
        for resource, action in ((self._context, "close"), (self._playwright, "stop")):
            if resource:
                try:
                    getattr(resource, action)()
                except Exception as e:
                    logger.debug(f"Error during scanner cleanup: {e}")
        self._context = self._playwright = self.page = None

    # -- public API -----------------------------------------------------

    def scan(self) -> ScanResult:
        """Full Phase 1 pipeline. Returns stats plus the newly stored jobs."""
        init_db()
        result = ScanResult(keywords=list(self.keywords))
        logger.info(
            f"Naukri scanner started | keywords={self.keywords} | locations={self.locations or '-'}"
            f"{' + all India' if self.search_all_locations else ''} | jobAge={self.job_age_days or '-'} | "
            f"experience={self.experience if self.experience is not None else '-'} | "
            f"max_pages={self.max_pages} | max_jobs={self.max_jobs}"
        )
        self.start()
        try:
            candidates = self.search_jobs(result)
            self._process_candidates(candidates, result)
        except ScanStopped as e:
            result.stopped_reason = str(e)
            logger.warning(f"Scan stopped: {e}")
        logger.info(f"Naukri scan completed: {result.summary()}")
        return result

    def search_queries(self) -> list[tuple[str, Optional[str]]]:
        """(keyword, location) in priority order: every keyword in each preferred location first,
        then (optionally) every keyword without a location. The job budget fills in this order."""
        queries = [(keyword, location) for keyword in self.keywords for location in self.locations]
        if self.search_all_locations or not self.locations:
            queries += [(keyword, None) for keyword in self.keywords]
        return queries

    def search_jobs(self, result: Optional[ScanResult] = None) -> list[NaukriJob]:
        """Walk search pages for every query; return unique jobs not already in the database that
        passed the search-card checks (not stale, experience minimum not above the resume)."""
        result = result or ScanResult(keywords=list(self.keywords))
        seen_keys: set[str] = set()
        candidates: list[NaukriJob] = []
        for keyword, location in self.search_queries():
            if len(candidates) >= self.max_jobs or result.pages_scanned >= self.max_search_pages:
                break
            logger.info(f"Searching Naukri for: {keyword} | {location or 'all locations'}")
            result.searches += 1
            self._search_keyword(keyword, seen_keys, candidates, result, location)
        result.unique_jobs = len(seen_keys)
        return candidates

    def fetch_job_details(self, job: NaukriJob) -> NaukriJob:
        """Open the job page and merge its details. An empty description is read again after waiting
        for the description element, then once more after one reload; still empty -> DescriptionMissing
        (the job is not stored, so the next scan tries it again). Never filled in from anything else."""
        self._navigate(job.url)
        self._wait_for(DETAIL_READY_SELECTOR, job)
        self._read_detail(job)
        if not job.job_description:
            logger.info(f"Description not rendered yet; waiting for it: {job.url}")
            self._wait_for(DESCRIPTION_SELECTOR, job)
            self._scroll_and_wait()
            self._read_detail(job)
        if not job.job_description:
            logger.info(f"Description still missing; reloading once: {job.url}")
            self._navigate(job.url)
            self._wait_for(DESCRIPTION_SELECTOR, job)
            self._read_detail(job)
        if not job.job_description:
            raise DescriptionMissing("job description not found on detail page (after waiting and one reload)")
        return job

    def _wait_for(self, selector: str, job: NaukriJob) -> None:
        try:
            self.page.wait_for_selector(selector, timeout=CONTENT_TIMEOUT_MS, state="attached")
        except PlaywrightError:
            self._check_page_state()
            logger.warning(f"Timeout waiting for job detail content: {job.url}")

    def _read_detail(self, job: NaukriJob) -> None:
        self._check_page_state()
        merge_detail(job, self.page.evaluate(DETAIL_EXTRACT_JS))

    def _scroll_and_wait(self) -> None:
        try:  # some layouts render the description lazily, once it is near the viewport
            self.page.evaluate("() => window.scrollTo(0, document.body ? document.body.scrollHeight / 2 : 0)")
        except PlaywrightError:
            pass
        self._sleep(DESCRIPTION_RETRY_WAIT_SECONDS)

    def card_skip_reason(self, job: NaukriJob, reference_time: datetime) -> Optional[str]:
        """Why a search card is not worth opening, or None. Only definite answers skip: a label that
        is already older than the window (an ambiguous one such as "1 day ago" is opened, its page may
        say more), or Naukri's own experience minimum above the verified resume experience. Missing,
        conflicting or unclear requirements are opened and decided on the full job."""
        status, _ = freshness_for(job.posted_label, reference_time=reference_time, window_hours=self.window_hours)
        if status == STALE:
            return "stale"
        card = {"title": job.title, "experience_min": job.experience_min, "experience_max": job.experience_max,
                "experience_text": job.experience_text}
        check = check_experience(card, self.verified_experience)
        if check.status == INELIGIBLE:
            return check.reason
        return None

    # -- internals ------------------------------------------------------

    def _search_keyword(
        self, keyword: str, seen_keys: set[str], candidates: list[NaukriJob], result: ScanResult,
        location: Optional[str] = None,
    ) -> None:
        page_signatures: set[frozenset] = set()
        search_url = build_search_url(keyword, location, self.experience, job_age=self.job_age_days)
        url = search_url
        for page_number in range(1, self.max_pages + 1):
            if len(candidates) >= self.max_jobs or result.pages_scanned >= self.max_search_pages:
                return
            logger.info(f"Scanning page {page_number} for '{keyword}': {url}")
            try:
                self._navigate(url)
            except PlaywrightError as e:
                logger.warning(f"Navigation failed for search page {url}: {e}")
                return
            raw_cards = self._extract_cards()
            result.pages_scanned += 1
            result.cards_found += len(raw_cards)
            jobs, malformed = parse_cards(raw_cards, keyword)
            result.malformed_cards += malformed

            signature = frozenset(job.dedup_key for job in jobs)
            if not signature or signature in page_signatures:
                logger.info(f"No jobs or repeated page for '{keyword}' at page {page_number}; stopping keyword")
                return
            page_signatures.add(signature)

            new_on_page = self._collect_new(jobs, seen_keys, candidates, result)
            if new_on_page == 0:
                logger.info(f"No unseen jobs on page {page_number} for '{keyword}'; stopping keyword")
                return
            url = next_page_url(self._next_page_href(), search_url)
            if not url:
                logger.info(f"No next page after page {page_number} for '{keyword}'")
                return

    def _collect_new(
        self, jobs: list[NaukriJob], seen_keys: set[str], candidates: list[NaukriJob], result: ScanResult
    ) -> int:
        unseen = 0
        now = datetime.now(timezone.utc)
        for job in jobs:
            key = job.dedup_key
            if key in seen_keys:
                result.duplicates_skipped += 1
                logger.debug(f"Duplicate within scan skipped: {key}")
                continue
            seen_keys.add(key)
            unseen += 1
            if job_exists(key, job.job_id, job.url):
                result.duplicates_skipped += 1
                logger.info(f"Duplicate skipped (already stored): {job.title} @ {job.company}")
                continue
            if len(candidates) >= self.max_jobs:
                continue
            skip = self.card_skip_reason(job, now)
            if skip == "stale":
                result.skipped_stale += 1
                logger.info(f"Skipped (posted {job.posted_label}, older than {self.window_hours}h): "
                            f"{job.title} @ {job.company}")
                continue
            if skip:
                result.skipped_experience += 1
                logger.info(f"{skip} | {job.title} @ {job.company}")
                continue
            candidates.append(job)
            logger.info(f"Job discovered: {job.title} @ {job.company} [{job.job_id or job.url}]")
        return unseen

    def _process_candidates(self, candidates: list[NaukriJob], result: ScanResult) -> None:
        for job in candidates:
            try:
                self.fetch_job_details(job)
            except ScanStopped:
                raise
            except DescriptionMissing as e:
                result.failed_job_pages += 1
                result.description_missing += 1
                logger.warning(f"Not stored (retried next scan): {job.url}: {e}")
                continue
            except Exception as e:
                result.failed_job_pages += 1
                logger.warning(f"Job detail extraction failed for {job.url}: {e}")
                continue
            if job.job_id and is_job_applied(job.job_id):
                job.is_already_applied = True
            now = datetime.now(timezone.utc)
            job.freshness_status, _ = freshness_for(job.posted_label, reference_time=now, window_hours=self.window_hours)
            exact = job.metadata.get("date_posted_exact")
            if job.freshness_status == UNKNOWN and exact:
                # The label alone is ambiguous ("1 day ago"); the page's exact timestamp settles it.
                job.posted_at = exact
                job.freshness_status, _ = freshness_for(reference_time=now, posted_at=exact,
                                                        window_hours=self.window_hours)
            job.freshness_checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                stored = save_discovered_job(job.to_dict())
            except Exception as e:
                logger.error(f"Database failure while storing {job.url}: {e}")
                raise
            if stored:
                result.new_jobs_stored += 1
                result.jobs.append(job)
                logger.info(f"Job stored: {job.title} @ {job.company}")
                self._record_experience(job)
            else:
                result.duplicates_skipped += 1
                logger.info(f"Duplicate skipped: {job.title} @ {job.company}")

    def _record_experience(self, job: NaukriJob) -> None:
        """Store the full-job experience check (with the description) on the new row right away."""
        try:
            row_id = find_job_row_id(job.dedup_key, job.job_id, job.url)
            row = get_job(row_id) if row_id is not None else None
            if row is not None:
                check = record_experience_check(row, self.verified_experience)
                if check.status != "ELIGIBLE":
                    logger.info(f"{check.reason} | {job.title} @ {job.company}")
        except Exception as e:  # noqa: BLE001 -- the pipeline checks again before export
            logger.warning(f"Experience check not stored for {job.url}: {e}")

    def _navigate(self, url: str) -> None:
        if self._navigations:
            self._sleep(random.uniform(*self.delay_range))
        self._navigations += 1
        self.page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        self._check_page_state()

    def _extract_cards(self) -> list[dict]:
        try:
            self.page.wait_for_selector(", ".join(CARD_SELECTORS), timeout=CONTENT_TIMEOUT_MS)
        except PlaywrightError:
            self._check_page_state()
            logger.warning(f"No job cards found on {self.page.url}")
            return []
        raw_cards = []
        for index, card in enumerate(find_card_handles(self.page)):
            try:
                raw_cards.append(card.evaluate(CARD_EXTRACT_JS))
            except PlaywrightError as e:
                logger.warning(f"Job card {index} extraction failed: {e}")
                raw_cards.append(None)
        return raw_cards

    def _next_page_href(self) -> Optional[str]:
        try:
            return self.page.evaluate(
                """() => {
                  const next = Array.from(document.querySelectorAll("[class*='pagination'] a"))
                    .find(a => /next/i.test(a.innerText || ''));
                  return next && !next.hasAttribute('disabled') ? next.getAttribute('href') : null;
                }"""
            )
        except PlaywrightError:
            return None

    def _check_page_state(self) -> None:
        url = self.page.url
        if is_login_page(url):
            raise ScanStopped(
                "Naukri requires login. Run the scan in headed mode, log in manually in the opened "
                "browser window (the profile is persisted), then re-run the scan."
            )
        try:
            title = self.page.title()
            body = self.page.evaluate("() => document.body ? document.body.innerText.slice(0, 2000) : ''")
            has_captcha_frame = bool(self.page.query_selector("iframe[src*='captcha'], iframe[src*='recaptcha']"))
        except PlaywrightError:
            return
        reason = "captcha iframe" if has_captcha_frame else detect_block(title, url, body)
        if reason:
            raise ScanStopped(
                f"Naukri returned a block/CAPTCHA page ({reason}). Not attempting to bypass it. "
                "If running headless, retry without --headless; otherwise wait and retry later."
            )


def _search_locations(values: list[str]) -> list[str]:
    """Configured search cities in order, without blanks, duplicates ("Bangalore" = "Bengaluru") or
    remote/WFH (not a city slug on Naukri; remote jobs come from the location-free search)."""
    seen, ordered = set(), []
    for value in values:
        name = str(value or "").strip()
        key = normalize_preferred_location(name)
        if not name or key in seen or key == "remote":
            continue
        seen.add(key)
        ordered.append(name)
    return ordered


__all__ = [
    "CARD_EXTRACT_JS",
    "CARD_SELECTORS",
    "NaukriScanner",
    "ScanResult",
    "ScanStopped",
    "DescriptionMissing",
    "find_card_handles",
    "launch_browser_context",
]
