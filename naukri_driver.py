import random
import logging
import time
from datetime import datetime
from typing import Optional
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from config import CHROME_USER_DATA, MATCH_THRESHOLD, DAILY_LIMIT, JOB_TITLES
from db import is_job_applied, log_job_application, get_applied_jobs_count
from naukri_parser import build_search_url, parse_card
from naukri_scanner import (
    CARD_EXTRACT_JS,
    CARD_SELECTORS,
    NaukriScanner,
    ScanResult,
    find_card_handles,
    launch_browser_context,
)
from resume_matcher import ResumeMatcher
from gemini_engine import GeminiEngine
from hitl_engine import HITLEngine

logger = logging.getLogger(__name__)


class NaukriDriver:
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
        self.page = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def start(self) -> None:
        self.playwright = sync_playwright().start()
        self.browser = launch_browser_context(self.playwright, self.headless)
        self.page = self.browser.new_page()

    def close(self) -> None:
        if self.page:
            self.page.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def scan_jobs(self, **scanner_options) -> ScanResult:
        """Phase 1 discovery using this driver's browser session (no applying)."""
        if not self.page:
            self.start()
        return NaukriScanner(page=self.page, **scanner_options).scan()

    def search_and_apply_jobs(
        self, keywords: Optional[list[str]] = None, max_limit: Optional[int] = None
    ) -> dict:
        if not self.page:
            self.start()

        keywords = keywords or JOB_TITLES
        max_limit = max_limit or DAILY_LIMIT

        results = {
            "applied": 0,
            "skipped": 0,
            "hitl": 0,
            "failed": 0,
            "total_checked": 0,
        }

        today = datetime.now().strftime("%Y-%m-%d")
        daily_count = get_applied_jobs_count(today)

        for keyword in keywords:
            if daily_count >= max_limit:
                logger.info(f"Daily limit ({max_limit}) reached")
                break

            logger.info(f"Searching Naukri for: {keyword}")
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

                    applied, hitl_triggered = self._try_apply_job(
                        card, job_data, job_description, match_score
                    )
                    if applied:
                        results["applied"] += 1
                        daily_count += 1
                    elif hitl_triggered:
                        results["hitl"] += 1
                    else:
                        results["failed"] += 1

                    time.sleep(random.uniform(3.0, 7.0))

                except Exception as e:
                    logger.error(f"Error processing job card {idx}: {e}")
                    results["failed"] += 1

        return results

    def _search_keyword(self, keyword: str) -> None:
        self.page.goto(build_search_url(keyword), wait_until="domcontentloaded")

    def _get_job_cards(self) -> list:
        try:
            self.page.wait_for_selector(", ".join(CARD_SELECTORS), timeout=15000)
            return find_card_handles(self.page)
        except Exception as e:
            logger.warning(f"Failed to get job cards: {e}")
            return []

    def _extract_job_data(self, card) -> Optional[dict]:
        try:
            job = parse_card(card.evaluate(CARD_EXTRACT_JS))
            if not job or not job.company:
                return None

            return {
                "id": job.job_id or job.url,
                "title": job.title,
                "company": job.company,
                "url": job.url,
                "platform": "Naukri",
            }
        except Exception as e:
            logger.error(f"Error extracting job data: {e}")
            return None

    def _get_job_description(self, card) -> str:
        try:
            card.click()
            time.sleep(1.0)

            desc_elem = self.page.query_selector("div.jobDesc")
            if desc_elem:
                return desc_elem.inner_text().strip()
            return ""
        except Exception as e:
            logger.warning(f"Failed to extract job description: {e}")
            return ""

    def _try_apply_job(
        self, card, job_data: dict, job_description: str, match_score: float
    ) -> tuple[bool, bool]:
        """Returns (applied, hitl_triggered)."""
        try:
            quick_apply_btn = card.query_selector("button.applyBtn, a.applyBtn")
            if quick_apply_btn:
                return self._apply_quick_apply(job_data, job_description, match_score)

            logger.info(f"No apply button found for job {job_data['id']}")
            return False, False

        except Exception as e:
            logger.error(f"Error applying to job: {e}")
            return False, False

    def _apply_quick_apply(
        self, job_data: dict, job_description: str, match_score: float
    ) -> tuple[bool, bool]:
        """Returns (applied, hitl_triggered)."""
        try:
            quick_apply_btn = self.page.query_selector("button.applyBtn, a.applyBtn")
            if quick_apply_btn:
                quick_apply_btn.click()
                time.sleep(1.0)

            if self._check_for_captcha_or_otp():
                self.hitl.request_human_intervention(
                    reason="CAPTCHA / OTP Detected",
                    job_details=job_data,
                )
                job_data["match_score"] = match_score
                job_data["status"] = "applied"
                job_data["applied_at"] = datetime.now().isoformat()
                log_job_application(job_data)
                return True, True

            success = self._handle_questionnaire(job_description)
            if success:
                job_data["match_score"] = match_score
                job_data["status"] = "applied"
                job_data["applied_at"] = datetime.now().isoformat()
                log_job_application(job_data)
                logger.info(f"Successfully applied to {job_data['id']}")
                return True, False

            return False, False
        except Exception as e:
            logger.error(f"Error in quick apply: {e}")
            return False, False

    def _check_for_captcha_or_otp(self) -> bool:
        try:
            captcha_indicators = [
                "div[class*='captcha']",
                "iframe[src*='captcha']",
                "div[class*='otp']",
                "input[placeholder*='OTP']",
                "div[class*='recaptcha']",
            ]
            for selector in captcha_indicators:
                if self.page.query_selector(selector):
                    return True
            return False
        except Exception:
            return False

    def _check_for_external_portal(self) -> bool:
        try:
            current_url = self.page.url
            if "naukri.com" not in current_url:
                return True

            external_redirect = self.page.query_selector(
                "a[href*='apply'], a[href*='careers'], a[href*='jobs']"
            )
            return external_redirect is not None
        except Exception:
            return False

    def _handle_questionnaire(self, job_description: str) -> bool:
        try:
            try:
                self.page.wait_for_selector(
                    "form, div.questionnaireForm, div[class*='question']", timeout=2000
                )
            except Exception:
                logger.debug("No questionnaire form found, assuming auto-applied")
                return True

            time.sleep(0.5)

            text_inputs = self.page.query_selector_all("input[type='text'], textarea")
            for field in text_inputs:
                field_label = self._get_field_label(field)
                if self.gemini and field_label:
                    answer = self.gemini.answer_screening_question(
                        self.matcher.resume_text or "", field_label
                    )
                    if answer:
                        field.fill(answer)
                        logger.debug(f"Filled field: {field_label}")

            radio_buttons = self.page.query_selector_all("input[type='radio']")
            for radio in radio_buttons:
                try:
                    radio.click()
                    time.sleep(0.2)
                except Exception:
                    pass

            submit_btn = self.page.query_selector(
                "button[type='submit'], button:has-text('Submit')"
            )
            if submit_btn:
                submit_btn.click()
                time.sleep(1.0)
                logger.info("Questionnaire submitted")
                return True

            return True

        except Exception as e:
            logger.error(f"Error handling questionnaire: {e}")
            return False

    def _get_field_label(self, field) -> str:
        try:
            label_elem = field.evaluate(
                "el => el.closest('div, .formField').querySelector('label')?.textContent || "
                "el.getAttribute('placeholder') || el.getAttribute('name') || ''"
            )
            return label_elem.strip() if label_elem else ""
        except Exception:
            return ""


__all__ = ["NaukriDriver"]
