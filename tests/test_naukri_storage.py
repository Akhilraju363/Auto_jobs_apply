import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
from naukri_parser import parse_card
from naukri_scanner import NaukriScanner, ScanResult

JOB_URL = "https://www.naukri.com/job-listings-java-developer-acme-pune-3-to-5-years-071025012345"


def make_card(job_id: str, title: str = "Java Developer") -> dict:
    return {
        "job_id": job_id,
        "url": f"https://www.naukri.com/job-listings-java-developer-acme-pune-{job_id}",
        "title": title,
        "company": "Acme",
        "location": "Pune",
        "experience": "3-5 Yrs",
    }


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite handles linger on Windows
        self.db_path = Path(self._tmp.name) / "jobs.db"
        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        db.init_db()


class TestJobStorage(TempDbTestCase):
    def test_insert_and_read_back(self):
        job = parse_card(make_card("100000000001"), "Java Developer")
        job.job_description = "Build Spring Boot services"
        job.skills = ["Java", "Spring Boot"]
        job.metadata = {"role": "Backend"}
        self.assertTrue(db.save_discovered_job(job.to_dict()))

        rows = db.get_jobs()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["status"], "discovered")
        self.assertEqual(row["job_id"], "100000000001")
        self.assertEqual(row["skills"], ["Java", "Spring Boot"])
        self.assertEqual(row["metadata"], {"role": "Backend"})
        self.assertIs(row["is_easy_apply"], False)
        self.assertIsNone(row["salary_min"])
        self.assertIsNone(row["match_score"])

    def test_duplicate_prevention_by_key_id_and_url(self):
        job = parse_card(make_card("100000000002"))
        self.assertTrue(db.save_discovered_job(job.to_dict()))
        self.assertFalse(db.save_discovered_job(job.to_dict()))

        same_id_other_key = dict(job.to_dict(), dedup_key="naukri:url:something-else")
        self.assertFalse(db.save_discovered_job(same_id_other_key))

        same_url_no_id = dict(job.to_dict(), job_id=None, dedup_key="naukri:url:" + job.url)
        self.assertFalse(db.save_discovered_job(same_url_no_id))

        self.assertEqual(len(db.get_jobs()), 1)
        self.assertTrue(db.job_exists(job.dedup_key))
        self.assertTrue(db.job_exists("unknown", job_id="100000000002"))
        self.assertFalse(db.job_exists("unknown", job_id="999"))

    def test_discovery_does_not_touch_application_history(self):
        db.log_job_application(
            {"id": "old1", "title": "t", "company": "c", "platform": "Naukri", "url": "u", "match_score": 50.0}
        )
        db.save_discovered_job(parse_card(make_card("100000000003")).to_dict())
        self.assertEqual(db.get_applied_jobs_count(), 1)
        self.assertFalse(db.is_job_applied("100000000003"))

    def test_migration_adds_missing_columns_and_keeps_rows(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TABLE jobs")
            conn.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT NOT NULL UNIQUE, "
                "source TEXT NOT NULL DEFAULT 'naukri', status TEXT NOT NULL DEFAULT 'discovered', "
                "first_seen_at TIMESTAMP NOT NULL, last_seen_at TIMESTAMP NOT NULL)"
            )
            conn.execute("INSERT INTO jobs (dedup_key, first_seen_at, last_seen_at) VALUES ('legacy', 'x', 'x')")
        db.init_db()
        with sqlite3.connect(self.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertTrue(set(db.JOB_COLUMNS) <= columns)
        self.assertEqual(count, 1)


class FakeScanner(NaukriScanner):
    """Scanner with the browser replaced by canned search pages."""

    def __init__(self, pages: dict, next_links: dict, failing_ids=(), **kwargs):
        super().__init__(page=object(), delay_range=(0, 0), sleep=lambda _: None, **kwargs)
        self.pages = pages
        self.next_links = next_links
        self.failing_ids = set(failing_ids)
        self.current = None

    def _navigate(self, url):
        self.current = url

    def _extract_cards(self):
        return self.pages.get(self.current, [])

    def _next_page_href(self):
        return self.next_links.get(self.current)

    def fetch_job_details(self, job):
        if job.job_id in self.failing_ids:
            raise TimeoutError("detail page timed out")
        job.job_description = f"JD for {job.job_id}"
        return job


class TestScannerFlow(TempDbTestCase):
    def setUp(self):
        super().setUp()
        base = "https://www.naukri.com/java-developer-jobs"
        page1 = [make_card("200000000001"), make_card("200000000002"), None]
        page2 = [make_card("200000000002"), make_card("200000000003")]
        self.pages = {
            f"{base}?k=Java+Developer": page1,
            f"{base}-2?k=Java+Developer": page2,
            f"{base}-3?k=Java+Developer": page2,  # repeated page -> must stop
            "https://www.naukri.com/software-engineer-jobs?k=Software+Engineer": [make_card("200000000001")],
        }
        self.next_links = {
            f"{base}?k=Java+Developer": "/java-developer-jobs-2",
            f"{base}-2?k=Java+Developer": "/java-developer-jobs-3",
            f"{base}-3?k=Java+Developer": "/java-developer-jobs-4",
        }

    def scanner(self, **kwargs):
        options = dict(keywords=["Java Developer", "Software Engineer"], max_pages=10, max_jobs=50, location="")
        options.update(kwargs)
        return FakeScanner(self.pages, self.next_links, **options)

    def test_scan_dedupes_paginates_and_continues_after_failures(self):
        result = self.scanner(failing_ids={"200000000002"}).scan()
        self.assertEqual(result.pages_scanned, 4)  # 3 Java pages (3rd repeated) + 1 SE page
        self.assertEqual(result.cards_found, 3 + 2 + 2 + 1)
        self.assertEqual(result.malformed_cards, 1)
        self.assertEqual(result.unique_jobs, 3)
        self.assertEqual(result.failed_job_pages, 1)
        self.assertEqual(result.new_jobs_stored, 2)
        self.assertEqual(sorted(j.job_id for j in result.jobs), ["200000000001", "200000000003"])
        self.assertTrue(all(j.status == "discovered" for j in result.jobs))

    def test_repeated_scan_creates_no_duplicates(self):
        self.scanner().scan()
        again = self.scanner().scan()
        self.assertEqual(again.new_jobs_stored, 0)
        self.assertEqual(len(db.get_jobs()), 3)
        self.assertGreaterEqual(again.duplicates_skipped, 3)

    def test_stops_when_next_page_unavailable(self):
        self.next_links.pop("https://www.naukri.com/java-developer-jobs?k=Java+Developer")
        result = self.scanner(keywords=["Java Developer"]).scan()
        self.assertEqual(result.pages_scanned, 1)

    def test_max_jobs_and_max_pages_limits(self):
        result = self.scanner(max_jobs=1).scan()
        self.assertEqual(result.new_jobs_stored, 1)
        result = self.scanner(keywords=["Java Developer"], max_pages=1).scan()
        self.assertEqual(result.pages_scanned, 1)

    def test_summary_shape(self):
        summary = ScanResult(keywords=["a"]).summary()
        self.assertEqual(summary["jobs"], 0)
        self.assertIn("new_jobs_stored", summary)


if __name__ == "__main__":
    unittest.main()
