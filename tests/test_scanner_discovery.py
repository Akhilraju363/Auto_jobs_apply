"""Discovery: search priority, Naukri's jobAge filter, search-card gating, description retries and the
freshness evidence the scanner records. The browser is a fake; nothing here opens Naukri."""

import unittest
from datetime import datetime, timedelta, timezone

import config
import db
import naukri_scanner as ns
from eligibility import FRESH, STALE, UNKNOWN, freshness_for
from experience_check import ELIGIBLE, INELIGIBLE, REVIEW_REQUIRED, VerifiedExperience, check_experience
from naukri_parser import build_search_url, merge_detail, parse_card, parse_posted_timestamp
from tests.test_naukri_storage import TempDbTestCase

RESUME = VerifiedExperience(4.0, "4+ years")
JD = "We need a Java developer with 3+ years of experience in Spring Boot, microservices and REST APIs."


def card(job_id, experience="3-5 Yrs", posted="Just now", title="Java Developer"):
    return {"job_id": job_id, "url": f"https://www.naukri.com/job-listings-java-developer-acme-bengaluru-{job_id}",
            "title": title, "company": "Acme", "location": "Bengaluru", "experience": experience, "posted": posted}


class DetailPage:
    """Answers DETAIL_EXTRACT_JS with scripted reads (the last one repeats)."""

    def __init__(self, reads):
        self.reads, self.count, self.scrolled = list(reads), 0, 0

    def wait_for_selector(self, *args, **kwargs):
        return None

    def evaluate(self, script, *args):
        if script == ns.DETAIL_EXTRACT_JS:
            self.count += 1
            return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        self.scrolled += 1
        return None


class Scanner(ns.NaukriScanner):
    def __init__(self, cards=(), reads=None, **kwargs):
        options = dict(keywords=["Java Developer"], location="", job_age_days=1, max_pages=1, max_jobs=50,
                       verified_experience=RESUME)
        options.update(kwargs)
        self.detail_page = DetailPage(reads or [{"description": JD}])
        super().__init__(page=self.detail_page, delay_range=(0, 0), sleep=lambda _: None, **options)
        self.cards, self.visited = list(cards), []

    def _navigate(self, url):
        self.visited.append(url)

    def _check_page_state(self):
        return None

    def _extract_cards(self):
        return self.cards

    def _next_page_href(self):
        return None


class TestSearchPriority(TempDbTestCase):  # scan() writes jobs: always a temporary DB
    def test_java_keywords_come_first(self):
        self.assertEqual(config.DEFAULT_NAUKRI_KEYWORDS[:4], ["Java Developer", "Java Spring Boot Developer",
                                                              "Java Backend Developer", "Java Full Stack Developer"])
        self.assertEqual(config.DEFAULT_NAUKRI_LOCATIONS, ["Bengaluru", "Chennai"])

    def test_preferred_cities_first_then_configured_then_all_india(self):
        scanner = ns.NaukriScanner(page=object(), keywords=["Java Developer", "Java Backend Developer"],
                                   locations=["Bengaluru", "Chennai", "", "Bangalore", "Hyderabad", "Remote"],
                                   search_all_locations=True, verified_experience=RESUME)
        self.assertEqual(scanner.locations, ["Bengaluru", "Chennai", "Hyderabad"])  # Bangalore = Bengaluru
        self.assertEqual(scanner.search_queries(), [
            ("Java Developer", "Bengaluru"), ("Java Developer", "Chennai"), ("Java Developer", "Hyderabad"),
            ("Java Backend Developer", "Bengaluru"), ("Java Backend Developer", "Chennai"),
            ("Java Backend Developer", "Hyderabad"),
            ("Java Developer", None), ("Java Backend Developer", None),
        ])

    def test_explicit_single_location_keeps_the_old_behaviour(self):
        scanner = ns.NaukriScanner(page=object(), keywords=["Java Developer"], location="Pune",
                                   verified_experience=RESUME)
        self.assertEqual(scanner.search_queries(), [("Java Developer", "Pune")])

    def test_job_age_filter_in_search_urls(self):
        self.assertEqual(build_search_url("Java Developer", "Bengaluru", job_age=1),
                         "https://www.naukri.com/java-developer-jobs-in-bengaluru?k=Java+Developer&jobAge=1")
        self.assertEqual(build_search_url("Java Developer"), "https://www.naukri.com/java-developer-jobs?k=Java+Developer")
        scanner = Scanner([card("300000000001")])
        scanner.scan()
        self.assertTrue(scanner.visited[0].endswith("?k=Java+Developer&jobAge=1"))

    def test_search_page_budget(self):
        scanner = Scanner([card("300000000001")], keywords=["A", "B", "C"], max_search_pages=2)
        self.assertEqual(scanner.search_jobs().__len__(), 1)
        self.assertEqual(len([u for u in scanner.visited if "-jobs" in u]), 2)


class TestSearchCardGate(TempDbTestCase):
    def test_stale_and_too_senior_cards_are_never_opened(self):
        cards = [card("300000000011", posted="2 days ago"), card("300000000012", posted="3+ weeks ago"),
                 card("300000000013", experience="5-10 Yrs"), card("300000000014", experience="6-8 Yrs"),
                 card("300000000015"), card("300000000016", posted="1 day ago"),  # ambiguous: opened
                 card("300000000017", experience=""),  # missing: opened, decided on the full job
                 card("300000000018", posted="4 hours ago", experience="2-4 Yrs")]
        scanner = Scanner(cards)
        result = scanner.scan()
        opened = [u for u in scanner.visited if "job-listings" in u]
        self.assertEqual(sorted(u[-2:] for u in opened), ["15", "16", "17", "18"])
        self.assertEqual((result.skipped_stale, result.skipped_experience, result.new_jobs_stored), (2, 2, 4))

    def test_skipped_cards_do_not_use_the_job_budget(self):
        cards = [card(f"30000000002{i}", experience="5-10 Yrs") for i in range(5)] + [card("300000000031")]
        result = Scanner(cards, max_jobs=1).scan()
        self.assertEqual(result.new_jobs_stored, 1)
        self.assertEqual(result.jobs[0].job_id, "300000000031")

    def test_no_verified_resume_skips_nothing_for_experience(self):
        result = Scanner([card("300000000041", experience="5-10 Yrs")], verified_experience=None).scan()
        self.assertEqual((result.skipped_experience, result.new_jobs_stored), (0, 1))
        self.assertEqual(db.get_jobs()[0]["experience_check_status"], REVIEW_REQUIRED)

    def test_full_job_experience_check_is_stored_at_scan_time(self):
        reads = [{"description": "Minimum of 7+ years of relevant engineering experience in Java."}]
        Scanner([card("300000000051")], reads=reads).scan()  # card 3-5, JD 7+ -> conflict
        row = db.get_jobs()[0]
        self.assertEqual(row["experience_check_status"], REVIEW_REQUIRED)
        self.assertIn("conflicting", row["experience_check_reason"])


class TestDescriptionRetries(TempDbTestCase):
    def test_late_description_is_read_after_waiting(self):
        scanner = Scanner([card("300000000061")], reads=[{"description": ""}, {"description": JD}])
        result = scanner.scan()
        self.assertEqual((result.new_jobs_stored, result.description_missing), (1, 0))
        self.assertEqual(scanner.detail_page.count, 2)
        self.assertEqual(scanner.detail_page.scrolled, 1)
        self.assertEqual(db.get_jobs()[0]["job_description"], JD)

    def test_description_after_one_reload(self):
        scanner = Scanner([card("300000000062")], reads=[{"description": ""}, {"description": ""}, {"description": JD}])
        self.assertEqual(scanner.scan().new_jobs_stored, 1)
        self.assertEqual(len([u for u in scanner.visited if "job-listings" in u]), 2)  # one reload

    def test_missing_description_is_not_stored_or_made_eligible(self):
        scanner = Scanner([card("300000000063")], reads=[{"description": "   "}])
        result = scanner.scan()
        self.assertEqual((result.new_jobs_stored, result.description_missing, result.failed_job_pages), (0, 1, 1))
        self.assertEqual(db.get_jobs(status=None), [])  # not stored: the next scan tries it again
        self.assertEqual(scanner.detail_page.count, 3)  # first read, after waiting, after one reload

    def test_json_ld_description_is_used_when_the_dom_has_none(self):
        ld = [{"@type": "JobPosting", "description": "<p>Java and Spring Boot, 3+ years of experience.</p>"}]
        result = Scanner([card("300000000064")], reads=[{"description": "", "ld_json": ld}]).scan()
        self.assertEqual(result.new_jobs_stored, 1)
        self.assertIn("Spring Boot", db.get_jobs()[0]["job_description"])


class TestMissingDescriptionRule(unittest.TestCase):
    def test_empty_description_is_review_not_eligible(self):
        row = {"title": "Java Developer", "experience_min": 3, "experience_max": 5, "job_description": ""}
        check = check_experience(row, RESUME)
        self.assertEqual(check.status, REVIEW_REQUIRED)
        self.assertIn("description could not be loaded", check.reason)
        self.assertEqual(check_experience(dict(row, job_description=None), RESUME).status, REVIEW_REQUIRED)

    def test_too_senior_is_still_skipped_without_a_description(self):
        row = {"title": "Java Developer", "experience_min": 6, "experience_max": 10, "job_description": ""}
        self.assertEqual(check_experience(row, RESUME).status, INELIGIBLE)

    def test_search_card_without_the_field_is_judged_on_naukri_data(self):
        self.assertEqual(check_experience({"title": "Java Developer", "experience_min": 3, "experience_max": 5},
                                          RESUME).status, ELIGIBLE)


class TestFreshnessEvidence(TempDbTestCase):
    REF = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)

    def test_week_and_month_labels_are_stale_not_unknown(self):
        for label in ("1 week ago", "2 weeks ago", "3+ weeks ago", "1 month ago", "30+ days ago"):
            with self.subTest(label):
                self.assertEqual(freshness_for(label, reference_time=self.REF)[0], STALE)
        self.assertEqual(freshness_for("1 day ago", reference_time=self.REF)[0], UNKNOWN)  # unchanged
        self.assertEqual(freshness_for("23 hours ago", reference_time=self.REF)[0], FRESH)

    def test_exact_timestamp_parsing(self):
        self.assertEqual(parse_posted_timestamp("2026-10-12T09:15:00Z"), "2026-10-12T09:15:00+00:00")
        self.assertEqual(parse_posted_timestamp("2026-10-12T09:15:00+05:30"), "2026-10-12T09:15:00+05:30")
        for vague in ("2026-10-12", "2026-10-12T09:15:00", "", None, "yesterday"):
            self.assertIsNone(parse_posted_timestamp(vague))

    def test_exact_timestamp_only_settles_an_ambiguous_label(self):
        now = datetime.now(timezone.utc)

        def stored(label, posted_hours_ago, job_id):
            exact = (now - timedelta(hours=posted_hours_ago)).isoformat()
            Scanner([card(job_id, posted=label)],
                    reads=[{"description": JD, "ld_json": [{"@type": "JobPosting", "datePosted": exact}]}]).scan()
            return next(j for j in db.get_jobs(status=None) if j["job_id"] == job_id)

        self.assertEqual(stored("1 day ago", 20, "300000000071")["freshness_status"], FRESH)
        self.assertEqual(stored("1 day ago", 30, "300000000072")["freshness_status"], STALE)
        recent = stored("Just now", 200, "300000000073")  # a reposted job: the label is not overridden
        self.assertEqual(recent["freshness_status"], FRESH)
        self.assertIsNone(recent["posted_at"])

    def test_merge_detail_keeps_the_timestamp_as_evidence_only(self):
        job = parse_card(card("300000000081"))
        merge_detail(job, {"description": JD, "ld_json": [{"@type": "JobPosting", "datePosted": "2026-10-12T09:00:00Z"}]})
        self.assertEqual(job.metadata["date_posted_exact"], "2026-10-12T09:00:00+00:00")
        self.assertIsNone(job.posted_at)


if __name__ == "__main__":
    unittest.main()
