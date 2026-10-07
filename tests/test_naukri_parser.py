import unittest
from datetime import date

from naukri_parser import (
    NaukriJob,
    build_search_url,
    classify_apply_buttons,
    clean_text,
    dedupe_jobs,
    detect_block,
    extract_job_id,
    is_login_page,
    make_dedup_key,
    merge_detail,
    next_page_url,
    normalize_job_url,
    parse_card,
    parse_cards,
    parse_experience,
    parse_posted_date,
    parse_salary,
)

JOB_URL = "https://www.naukri.com/job-listings-java-developer-acme-pune-3-to-5-years-071025012345"

VALID_CARD = {
    "job_id": "071025012345",
    "url": JOB_URL + "?src=srp&sid=123",
    "title": "  Java Developer ",
    "company": "Acme",
    "location": "Pune, Chennai, Mumbai (All Areas) ",
    "experience": "3-5 Yrs",
    "salary": "Not disclosed",
    "posted": "2 days ago",
    "tags": ["Java", "Spring Boot", "java", " Microservices "],
}


class TestUrlNormalization(unittest.TestCase):
    def test_strips_query_fragment_and_trailing_slash(self):
        self.assertEqual(normalize_job_url(JOB_URL + "/?src=jddesktop#apply"), JOB_URL)

    def test_relative_and_protocol_relative_urls(self):
        self.assertEqual(normalize_job_url("/job-listings-x-123456789"), "https://www.naukri.com/job-listings-x-123456789")
        self.assertEqual(normalize_job_url("//www.naukri.com/job-listings-x-123456789"), "https://www.naukri.com/job-listings-x-123456789")

    def test_forces_https_and_lowercase_host(self):
        self.assertEqual(normalize_job_url("http://WWW.Naukri.com/abc"), "https://www.naukri.com/abc")

    def test_invalid_urls(self):
        for value in (None, "", "   ", "javascript:void(0)", "mailto:a@b.com"):
            self.assertIsNone(normalize_job_url(value), value)

    def test_extract_job_id(self):
        self.assertEqual(extract_job_id(JOB_URL), "071025012345")
        self.assertEqual(extract_job_id("https://www.naukri.com/x?jobId=998877665544"), "998877665544")
        self.assertEqual(extract_job_id(JOB_URL, explicit_id="111222333444"), "111222333444")
        self.assertIsNone(extract_job_id("https://www.naukri.com/java-developer-jobs-2"))
        self.assertIsNone(extract_job_id(None))


class TestSearchUrl(unittest.TestCase):
    def test_keyword_only(self):
        self.assertEqual(
            build_search_url("Java Full Stack Developer"),
            "https://www.naukri.com/java-full-stack-developer-jobs?k=Java+Full+Stack+Developer",
        )

    def test_location_experience_and_page(self):
        self.assertEqual(
            build_search_url("Java Developer", "Bangalore", 3, page=2),
            "https://www.naukri.com/java-developer-jobs-in-bangalore-2?k=Java+Developer&experience=3",
        )

    def test_next_page_follows_canonical_slug_and_keeps_filters(self):
        search = build_search_url("Java Developer", "Hyderabad", 3)
        self.assertEqual(
            next_page_url("/java-developer-jobs-in-hyderabad-secunderabad-2", search),
            "https://www.naukri.com/java-developer-jobs-in-hyderabad-secunderabad-2?k=Java+Developer&experience=3",
        )
        self.assertIsNone(next_page_url(None, search))
        self.assertIsNone(next_page_url("https://evil.example.com/x", search))


class TestExperienceParsing(unittest.TestCase):
    def test_variants(self):
        cases = {
            "3-5 Yrs": (3, 5),
            "5 - 10 years": (5, 10),
            "10+ Yrs": (10, None),
            "2 Yrs": (2, 2),
            "Fresher": (0, 0),
            "0-1 Yrs": (0, 1),
            "": (None, None),
            None: (None, None),
            "Not mentioned": (None, None),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_experience(text), expected, text)


class TestSalaryParsing(unittest.TestCase):
    def test_variants(self):
        cases = {
            "15-27.5 Lacs P.A.": (1_500_000, 2_750_000),
            "3.5-5 Lacs PA": (350_000, 500_000),
            "1-1.5 Cr P.A.": (10_000_000, 15_000_000),
            "₹ 6,00,000 - 8,00,000 P.A.": (600_000, 800_000),
            "50,000-60,000 per month": (600_000, 720_000),
            "Up to 10 Lacs P.A.": (None, 1_000_000),
            "12 LPA": (1_200_000, 1_200_000),
            "Not disclosed": (None, None),
            "": (None, None),
            None: (None, None),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_salary(text), expected, text)


class TestPostedDate(unittest.TestCase):
    TODAY = date(2026, 10, 7)

    def test_variants(self):
        cases = {
            "2026-09-25": "2026-09-25",
            "Just now": "2026-10-07",
            "Today": "2026-10-07",
            "Few hours ago": "2026-10-07",
            "1 day ago": "2026-10-06",
            "3 Days Ago": "2026-10-04",
            "30+ Days Ago": None,
            "1 week ago": None,
            "": None,
        }
        for text, expected in cases.items():
            self.assertEqual(parse_posted_date(text, self.TODAY), expected, text)


class TestCardParsing(unittest.TestCase):
    def test_valid_card_is_normalized(self):
        job = parse_card(VALID_CARD, "Java Developer")
        self.assertEqual(job.title, "Java Developer")
        self.assertEqual(job.url, JOB_URL)
        self.assertEqual(job.job_id, "071025012345")
        self.assertEqual(job.location, "Pune, Chennai, Mumbai (All Areas)")
        self.assertEqual((job.experience_min, job.experience_max), (3, 5))
        self.assertEqual((job.salary_min, job.salary_max), (None, None))
        self.assertEqual(job.skills, ["Java", "Spring Boot", "Microservices"])
        self.assertEqual(job.search_keyword, "Java Developer")
        self.assertEqual(job.status, "discovered")
        self.assertEqual(job.source, "naukri")
        self.assertFalse(job.is_already_applied)

    def test_missing_optional_fields_become_empty_not_fake(self):
        job = parse_card({"url": JOB_URL, "title": "Engineer"})
        self.assertEqual(job.company, "")
        self.assertEqual(job.location, "")
        self.assertIsNone(job.experience_min)
        self.assertIsNone(job.salary_max)
        self.assertIsNone(job.posted_date)
        self.assertEqual(job.skills, [])

    def test_malformed_cards_are_skipped_and_counted(self):
        raw = [VALID_CARD, None, "garbage", {"title": "No URL"}, {"url": JOB_URL}, {"url": "javascript:void(0)", "title": "x"}]
        jobs, malformed = parse_cards(raw)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(malformed, 5)

    def test_parse_cards_survives_exceptions(self):
        class Exploding(dict):
            def get(self, *args):
                raise RuntimeError("boom")

        jobs, malformed = parse_cards([Exploding(), VALID_CARD])
        self.assertEqual((len(jobs), malformed), (1, 1))


class TestDeduplication(unittest.TestCase):
    def test_key_priority(self):
        self.assertEqual(make_dedup_key("123456789", JOB_URL, "a", "b", "c"), "naukri:id:123456789")
        self.assertEqual(make_dedup_key(None, JOB_URL + "?x=1"), f"naukri:url:{JOB_URL}")
        self.assertEqual(
            make_dedup_key(None, None, " Java  Dev ", "ACME", "Pune"), "naukri:tcl:java dev|acme|pune"
        )

    def test_same_job_from_different_pages_and_keywords(self):
        a = parse_card(VALID_CARD, "Java Developer")
        b = parse_card(dict(VALID_CARD, url=JOB_URL + "?src=other"), "Software Engineer")
        c = parse_card(dict(VALID_CARD, job_id="", url=JOB_URL.replace("012345", "999999")))
        unique = dedupe_jobs([a, b, c])
        self.assertEqual([j.job_id for j in unique], ["071025012345", "071025999999"])


class TestDetailMerge(unittest.TestCase):
    LD = {
        "@type": "JobPosting",
        "title": "Java Developer",
        "description": "<p>Role &amp; responsibilities</p><ul><li>Build APIs</li><li>Write tests</li></ul>",
        "identifier": {"value": "071025012345"},
        "datePosted": "2026-09-25",
        "experienceRequirements": {"monthsOfExperience": "60"},
        "skills": ["core java", " JSON "],
        "employmentType": "Full Time, Permanent",
        "hiringOrganization": {"name": "Acme Corp"},
        "jobLocation": {"address": {"addressLocality": ["Bengaluru", "Bengaluru"]}},
        "baseSalary": {"value": {"value": "15-27.5 Lacs P.A "}},
    }

    def test_dom_values_win_and_description_keeps_structure(self):
        job = parse_card(VALID_CARD)
        detail = {
            "title": "Senior Java Developer",
            "company": "Acme",
            "location": "Bangalore Rural, Bengaluru",
            "experience": "5 - 10 years",
            "salary": "",
            "description": "Role & responsibilities\n\n\n\n  Must   have Java  \n- Spring",
            "skills": ["Java", "SQL"],
            "details": {"Employment Type": "Full Time, Permanent", "Role": "Software Development", "Posted": "1 week ago"},
            "buttons": [{"id": "apply-button", "text": "Apply", "href": ""}],
            "work_mode": "Hybrid",
            "ld_json": [self.LD, {"@type": "BreadcrumbList"}],
        }
        merge_detail(job, detail, today=date(2026, 10, 7))
        self.assertEqual(job.title, "Senior Java Developer")
        self.assertEqual(job.location, "Bangalore Rural, Bengaluru")
        self.assertEqual((job.experience_min, job.experience_max), (5, 10))
        self.assertEqual((job.salary_min, job.salary_max), (1_500_000, 2_750_000))  # from JSON-LD
        self.assertEqual(job.job_description, "Role & responsibilities\n\nMust have Java\n- Spring")
        self.assertEqual(job.skills, ["Java", "SQL"])
        self.assertEqual(job.employment_type, "Full Time, Permanent")
        self.assertEqual(job.posted_date, "2026-09-25")
        self.assertEqual(job.posted_label, "1 week ago")
        self.assertTrue(job.is_easy_apply and job.has_apply_button)
        self.assertEqual(job.apply_url, job.url)
        self.assertEqual(job.metadata, {"role": "Software Development", "work_mode": "Hybrid"})

    def test_json_ld_fills_gaps_when_dom_changed(self):
        job = parse_card({"url": JOB_URL, "title": "Java Dev"})
        merge_detail(job, {"ld_json": [self.LD]})
        self.assertEqual(job.company, "Acme Corp")
        self.assertEqual(job.location, "Bengaluru")
        self.assertEqual(job.experience_min, 5)
        self.assertIn("- Build APIs", job.job_description)
        self.assertIn("Role & responsibilities", job.job_description)
        self.assertEqual(job.skills, ["core java", "JSON"])
        self.assertFalse(job.has_apply_button)
        self.assertIsNone(job.apply_url)

    def test_empty_detail_keeps_card_data(self):
        job = parse_card(VALID_CARD)
        merge_detail(job, {})
        self.assertEqual(job.company, "Acme")
        self.assertEqual(job.job_description, "")

    def test_to_dict_contains_phase2_fields(self):
        data = parse_card(VALID_CARD).to_dict()
        for key in ("job_id", "title", "company", "location", "experience_min", "experience_max", "salary_min",
                    "salary_max", "employment_type", "job_description", "skills", "posted_date", "source", "url",
                    "apply_url", "is_easy_apply", "is_already_applied", "scraped_at", "status", "dedup_key"):
            self.assertIn(key, data)


class TestPageState(unittest.TestCase):
    def test_apply_buttons(self):
        self.assertTrue(classify_apply_buttons([{"id": "already-applied", "text": "Applied"}])["is_already_applied"])
        external = classify_apply_buttons([{"id": "company-site-button", "text": "Apply on company site", "href": "https://careers.acme.com/1"}])
        self.assertEqual(external, {"has_apply_button": True, "is_easy_apply": False, "is_already_applied": False,
                                    "apply_url": "https://careers.acme.com/1"})
        self.assertFalse(classify_apply_buttons([None, {"id": "x", "text": "Login"}])["has_apply_button"])

    def test_block_and_login_detection(self):
        self.assertEqual(detect_block(title="Access Denied"), "access denied")
        self.assertIsNotNone(detect_block(body="Please verify you are human"))
        self.assertIsNone(detect_block(title="Java Developer Jobs - Naukri.com", url=JOB_URL, body="Jobs | Companies"))
        self.assertTrue(is_login_page("https://www.naukri.com/nlogin/login?URL=x"))
        self.assertFalse(is_login_page(JOB_URL))

    def test_clean_text(self):
        self.assertEqual(clean_text("a  b\r\n\r\n\r\n\r\nc  "), "a b\n\nc")
        self.assertEqual(clean_text(None), "")


if __name__ == "__main__":
    unittest.main()
