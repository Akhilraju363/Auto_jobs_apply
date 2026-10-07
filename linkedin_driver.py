import random
import logging
import time
from datetime import datetime
from typing import Optional
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from config import CHROME_USER_DATA, MATCH_THRESHOLD, DAILY_LIMIT, JOB_TITLES
from db import is_job_applied, log_job_application, get_applied_jobs_count
from resume_matcher import ResumeMatcher
from gemini_engine import GeminiEngine
from hitl_engine import HITLEngine

logger = logging.getLogger(__name__)

LINKEDIN_BASE_URL = "https://www.linkedin.com/jobs/search/"


class LinkedInDriver:
    def __init__(self, headless: bool = True, hitl_engine: Optional[HITLEngine] = None):
        self.headless = headless
        self.user_data_dir = str(CHROME_USER_DATA)
        self.matcher = ResumeMatcher()
        try:
            self.gemini = GeminiEngine()
        except ValueError:
            self.gemini = None
            logger.warning("GeminiEngine not initialized")
        self.hitl = hitl_engine or HITLEngine()
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def start(self) -> None:
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        self.page = self.browser.new_page()

    def close(self) -> None:
        if self.page:
            self.page.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def search_and_apply_jobs(self, keywords: Optional[list[str]] = None, max_limit: Optional[int] = None) -> dict:
        if not self.page:
            self.start()

        keywords = keywords or JOB_TITLES
        max_limit = max_limit or DAILY_LIMIT

        results = {
            "applied": 0,
            "skipped": 0,
            "external": 0,
            "failed": 0,
            "total_checked": 0,
        }

        today = datetime.now().strftime("%Y-%m-%d")
        daily_count = get_applied_jobs_count(today)

        for keyword in keywords:
            if daily_count >= max_limit:
                logger.info(f"Daily limit ({max_limit}) reached")
                break

            logger.info(f"Searching for: {keyword}")
            self._search_keyword(keyword)
            time.sleep(random.uniform(2.0, 4.0))

            job_cards = self._get_job_cards()
            logger.info(f"Found {len(job_cards)} job cards")

            for idx, card in enumerate(job_cards):
                if daily_count >= max_limit:
                    logger.info(f"Daily limit reached after {results['applied']} applications")
                    break

                try:
                    job_data = self._extract_job_data(card)
                    if not job_data:
                        results["failed"] += 1
                        continue

                    results["total_checked"] += 1

                    if is_job_applied(job_data["id"]):
                        logger.debug(f"Job {job_data['id']} already applied")
                        results["skipped"] += 1
                        continue

                    job_description = self._get_job_description(card)
                    match_score = self.matcher.calculate_match_score(job_description)

                    logger.info(
                        f"Job: {job_data['title']} @ {job_data['company']} | "
                        f"Score: {match_score}% | Match: {'✓' if match_score >= MATCH_THRESHOLD else '✗'}"
                    )

                    if match_score < MATCH_THRESHOLD:
                        results["skipped"] += 1
                        time.sleep(random.uniform(1.0, 2.0))
                        continue

                    applied = self._try_apply_job(card, job_data, job_description, match_score)
                    if applied:
                        results["applied"] += 1
                        daily_count += 1
                    else:
                        results["failed"] += 1

                    time.sleep(random.uniform(3.0, 7.0))

                except Exception as e:
                    logger.error(f"Error processing job card {idx}: {e}")
                    results["failed"] += 1

        return results

    def _search_keyword(self, keyword: str) -> None:
        url = f"{LINKEDIN_BASE_URL}?keywords={keyword}&sortBy=DD&f_TPR=r86400"
        self.page.goto(url, wait_until="networkidle")

    def _get_job_cards(self) -> list:
        try:
            self.page.wait_for_selector("div[data-job-id]", timeout=5000)
            cards = self.page.query_selector_all("div[data-job-id]")
            return cards
        except Exception as e:
            logger.warning(f"Failed to get job cards: {e}")
            return []

    def _extract_job_data(self, card) -> Optional[dict]:
        try:
            job_id = card.get_attribute("data-job-id")
            title_elem = card.query_selector("h3.base-search-card__title")
            company_elem = card.query_selector("h4.base-search-card__subtitle")
            url_elem = card.query_selector("a.base-card__full-link")

            if not all([job_id, title_elem, company_elem, url_elem]):
                return None

            return {
                "id": job_id,
                "title": title_elem.inner_text().strip(),
                "company": company_elem.inner_text().strip(),
                "url": url_elem.get_attribute("href"),
                "platform": "LinkedIn",
            }
        except Exception as e:
            logger.error(f"Error extracting job data: {e}")
            return None

    def _get_job_description(self, card) -> str:
        try:
            card.click()
            time.sleep(1.0)

            desc_elem = self.page.query_selector(
                "div.show-more-less-html__markup, div.description__text"
            )
            if desc_elem:
                return desc_elem.inner_text().strip()
            return ""
        except Exception as e:
            logger.warning(f"Failed to extract job description: {e}")
            return ""

    def _try_apply_job(
        self, card, job_data: dict, job_description: str, match_score: float
    ) -> bool:
        try:
            easy_apply_btn = card.query_selector("button[aria-label*='Easy Apply']")
            if easy_apply_btn:
                return self._apply_easy_apply(job_data, job_description, match_score)

            external_link = self._get_external_apply_link(card)
            if external_link:
                return self._log_external_job(job_data, external_link, match_score)

            logger.info(f"No apply button found for job {job_data['id']}")
            return False

        except Exception as e:
            logger.error(f"Error applying to job: {e}")
            return False

    def _apply_easy_apply(self, job_data: dict, job_description: str, match_score: float) -> bool:
        try:
            easy_apply_btn = self.page.query_selector("button[aria-label*='Easy Apply']")
            if easy_apply_btn:
                easy_apply_btn.click()
                time.sleep(1.0)

            success = self._handle_easy_apply_modal(job_description)
            if success:
                job_data["match_score"] = match_score
                job_data["status"] = "applied"
                job_data["applied_at"] = datetime.now().isoformat()
                log_job_application(job_data)
                logger.info(f"Successfully applied to {job_data['id']}")
                return True

            return False
        except Exception as e:
            logger.error(f"Error in easy apply: {e}")
            return False

    def _handle_easy_apply_modal(self, job_description: str) -> bool:
        try:
            modal_selector = "div[role='dialog'], div.artdeco-modal"
            self.page.wait_for_selector(modal_selector, timeout=3000)

            max_steps = 10
            step = 0

            while step < max_steps:
                step += 1
                time.sleep(0.5)

                form_fields = self.page.query_selector_all(
                    "input[type='text'], input[type='radio'], textarea, select"
                )

                for field in form_fields:
                    field_type = field.get_attribute("type") or field.tag_name
                    field_name = field.get_attribute("name") or field.get_attribute("id") or ""
                    field_label = self._get_field_label(field)

                    if field_type == "text" or field.tag_name == "textarea":
                        if self.gemini and field_label:
                            answer = self.gemini.answer_screening_question(
                                self._get_resume_text(), field_label
                            )
                            if answer:
                                field.fill(answer)
                                logger.debug(f"Filled field: {field_label}")
                        else:
                            field.fill("")

                    elif field_type == "radio":
                        try:
                            field.click()
                        except:
                            pass

                submit_btn = self.page.query_selector(
                    "button[aria-label*='Submit'], button:has-text('Submit Application')"
                )
                if submit_btn:
                    submit_btn.click()
                    time.sleep(1.0)
                    logger.info("Application submitted")
                    return True

                next_btn = self.page.query_selector(
                    "button[aria-label*='Next'], button:has-text('Next')"
                )
                if next_btn:
                    next_btn.click()
                    time.sleep(0.8)
                    continue

                review_btn = self.page.query_selector(
                    "button[aria-label*='Review'], button:has-text('Review')"
                )
                if review_btn:
                    review_btn.click()
                    time.sleep(0.8)
                    continue

                if not form_fields and not next_btn and not submit_btn:
                    logger.warning("Modal form completed or unrecognized structure")
                    break

            logger.warning(f"Modal handling did not complete in {max_steps} steps")
            return False

        except Exception as e:
            logger.error(f"Error handling easy apply modal: {e}")
            return False

    def _get_field_label(self, field) -> str:
        label_elem = field.evaluate(
            "el => el.closest('div').querySelector('label')?.textContent || ''"
        )
        return label_elem.strip() if label_elem else ""

    def _get_resume_text(self) -> str:
        return self.matcher.resume_text or ""

    def _get_external_apply_link(self, card) -> Optional[str]:
        try:
            external_link = card.query_selector(
                "a[href*='apply'], a[aria-label*='Apply on company website']"
            )
            if external_link:
                return external_link.get_attribute("href")
        except:
            pass
        return None

    def _log_external_job(self, job_data: dict, external_url: str, match_score: float) -> bool:
        try:
            job_data["url"] = external_url
            job_data["match_score"] = match_score
            job_data["status"] = "EXTERNAL_PENDING"
            job_data["applied_at"] = datetime.now().isoformat()
            log_job_application(job_data)
            logger.info(f"Logged external job: {job_data['id']} - {external_url}")
            return True
        except Exception as e:
            logger.error(f"Error logging external job: {e}")
            return False


__all__ = ["LinkedInDriver"]
