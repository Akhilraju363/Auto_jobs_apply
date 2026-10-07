"""Pre-Phase-2 hardening: repository safety, scoring schema, data contract, resume input."""

import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import db
from naukri_parser import parse_card
from resume_matcher import ResumeMatcher

REPO_ROOT = Path(__file__).resolve().parent.parent

# jobs table exactly as Phase 1 created it (before scoring columns existed)
PHASE1_JOBS_SCHEMA = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'naukri',
    status TEXT NOT NULL DEFAULT 'discovered',
    first_seen_at TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP NOT NULL,
    job_id TEXT, title TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '',
    location TEXT, experience_min INTEGER, experience_max INTEGER, experience_text TEXT,
    salary_min INTEGER, salary_max INTEGER, salary_text TEXT, employment_type TEXT,
    job_description TEXT, skills TEXT, posted_date TEXT, posted_label TEXT,
    url TEXT NOT NULL DEFAULT '', apply_url TEXT,
    is_easy_apply INTEGER NOT NULL DEFAULT 0, is_already_applied INTEGER NOT NULL DEFAULT 0,
    has_apply_button INTEGER NOT NULL DEFAULT 0, search_keyword TEXT, metadata TEXT,
    match_score REAL, scraped_at TIMESTAMP
)
"""
APPLIED_JOBS_SCHEMA = """
CREATE TABLE applied_jobs (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, platform TEXT NOT NULL,
    url TEXT NOT NULL, match_score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    applied_at TIMESTAMP NOT NULL
)
"""

CONTRACT_FIELDS = (
    "job_id", "title", "company", "location", "experience_min", "experience_max", "salary_min",
    "salary_max", "employment_type", "job_description", "skills", "posted_date", "source", "url",
    "apply_url", "is_easy_apply", "is_already_applied", "status", "match_score", "match_status",
    "match_reason", "matching_skills", "missing_skills", "experience_match", "location_match", "scored_at",
)
SCORING_FIELDS = ("match_score", "match_status", "match_reason", "experience_match", "location_match", "scored_at")


def make_job(job_id: str, description: str = "Java Spring Boot microservices REST APIs") -> dict:
    job = parse_card(
        {
            "job_id": job_id,
            "url": f"https://www.naukri.com/job-listings-java-developer-acme-pune-{job_id}",
            "title": "Java Developer",
            "company": "Acme",
            "location": "Pune",
            "experience": "3-5 Yrs",
            "tags": ["Java", "Spring Boot"],
        }
    )
    job.job_description = description
    return job.to_dict()


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


@unittest.skipUnless(shutil.which("git") and (REPO_ROOT / ".git").exists(), "git repository required")
class TestRepositorySafety(unittest.TestCase):
    def test_sensitive_paths_are_ignored(self):
        for path in (".env", "chrome_user_data/Default/Cookies", "__pycache__/db.cpython-314.pyc",
                     "tests/__pycache__/x.pyc", ".pytest_cache/v/cache"):
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT)
            self.assertEqual(result.returncode, 0, f"{path} is not ignored")

    def test_secrets_and_session_data_not_tracked(self):
        self.assertEqual(git("ls-files", ".env").strip(), "")
        self.assertEqual(git("ls-files", "chrome_user_data").strip(), "")

    def test_env_example_has_no_real_key(self):
        example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        key_line = next(line for line in example.splitlines() if line.startswith("GEMINI_API_KEY="))
        self.assertEqual(key_line, "GEMINI_API_KEY=your_gemini_api_key_here")


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite handles linger on Windows
        self.db_path = Path(self._tmp.name) / "jobs.db"
        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def columns(self) -> set[str]:
        with sqlite3.connect(self.db_path) as conn:
            return {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}


class TestScoringMigration(TempDbTestCase):
    def create_phase1_database(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(PHASE1_JOBS_SCHEMA)
            conn.execute(APPLIED_JOBS_SCHEMA)
            conn.execute(
                "INSERT INTO jobs (dedup_key, job_id, title, url, job_description, skills, first_seen_at, last_seen_at) "
                "VALUES ('naukri:id:300000000001', '300000000001', 'Old Job', "
                "'https://www.naukri.com/job-listings-old-300000000001', 'Old JD', '[\"Java\"]', 't0', 't0')"
            )
            conn.execute(
                "INSERT INTO applied_jobs VALUES ('a1', 'Applied', 'Co', 'Naukri', 'u', 55.0, 'applied', '2026-10-01T10:00:00')"
            )

    def test_phase1_database_gains_scoring_columns_without_data_loss(self):
        self.create_phase1_database()
        self.assertNotIn("match_status", self.columns())

        db.init_db()
        db.init_db()  # repeat initialization is a no-op

        self.assertTrue(set(db.SCORING_COLUMNS) | set(db.JOB_COLUMNS) <= self.columns())
        jobs = db.get_jobs()
        self.assertEqual(len(jobs), 1)
        old = jobs[0]
        self.assertEqual((old["title"], old["job_description"], old["skills"]), ("Old Job", "Old JD", ["Java"]))
        self.assertEqual(old["status"], "discovered")
        for field in SCORING_FIELDS:
            self.assertIsNone(old[field], field)
        self.assertEqual((old["matching_skills"], old["missing_skills"]), ([], []))

        self.assertEqual(db.get_applied_jobs_count(), 1)
        self.assertEqual(db.get_applied_jobs_count("2026-10-01"), 1)
        self.assertTrue(db.is_job_applied("a1"))

    def test_dedup_still_works_after_migration(self):
        self.create_phase1_database()
        db.init_db()
        old_url = "https://www.naukri.com/job-listings-old-300000000001"
        same_id = dict(make_job("300000000001"), dedup_key="naukri:url:other")
        same_url = dict(make_job("399999999999"), url=old_url, dedup_key="naukri:id:399999999999", job_id="399999999999")
        self.assertFalse(db.save_discovered_job(same_id))
        self.assertFalse(db.save_discovered_job(same_url))
        self.assertTrue(db.save_discovered_job(make_job("300000000002")))
        self.assertEqual(len(db.get_jobs()), 2)

    def test_init_db_twice_on_fresh_database(self):
        db.init_db()
        db.init_db()
        db.save_discovered_job(make_job("300000000003"))
        db.init_db()
        self.assertEqual(len(db.get_jobs()), 1)


class TestJobContract(TempDbTestCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def test_discovered_job_contract(self):
        db.save_discovered_job(make_job("400000000001"))
        job = db.get_jobs(status="discovered")[0]
        missing = [field for field in CONTRACT_FIELDS if field not in job]
        self.assertEqual(missing, [])
        self.assertEqual(job["status"], "discovered")
        self.assertEqual(job["source"], "naukri")
        self.assertIsInstance(job["skills"], list)
        self.assertIsInstance(job["is_easy_apply"], bool)
        for field in SCORING_FIELDS:
            self.assertIsNone(job[field], field)

    def test_discovery_ignores_scoring_fields_passed_in(self):
        job = dict(make_job("400000000002"), match_score=99, match_status="recommended", status="scored")
        db.save_discovered_job(job)
        stored = db.get_jobs()[0]
        self.assertEqual(stored["status"], "discovered")
        self.assertIsNone(stored["match_score"])
        self.assertIsNone(stored["match_status"])

    def test_save_job_score_moves_job_to_scored(self):
        db.save_discovered_job(make_job("400000000003"))
        row_id = db.get_jobs()[0]["id"]
        self.assertTrue(
            db.save_job_score(row_id, 72.5, "recommended", "Strong Java overlap", ["Java"], ["Kafka"], True, None)
        )
        self.assertEqual(db.get_jobs(status="discovered"), [])
        scored = db.get_jobs(status="scored")[0]
        self.assertEqual(scored["match_score"], 72.5)
        self.assertEqual(scored["match_status"], "recommended")
        self.assertEqual(scored["match_reason"], "Strong Java overlap")
        self.assertEqual((scored["matching_skills"], scored["missing_skills"]), (["Java"], ["Kafka"]))
        self.assertIs(scored["experience_match"], True)
        self.assertIsNone(scored["location_match"])
        self.assertIsNotNone(scored["scored_at"])
        self.assertEqual(db.get_applied_jobs_count(), 0)

    def test_save_job_score_validation(self):
        db.save_discovered_job(make_job("400000000004"))
        row_id = db.get_jobs()[0]["id"]
        with self.assertRaises(ValueError):
            db.save_job_score(row_id, 101, "recommended")
        with self.assertRaises(ValueError):
            db.save_job_score(row_id, 50, "applied")
        self.assertFalse(db.save_job_score(999999, 50, "review"))
        self.assertEqual(db.get_jobs()[0]["status"], "discovered")


class TestResumeInput(unittest.TestCase):
    def test_resume_path_resolution(self):
        self.assertEqual(config.resolve_project_path("resume.txt"), config.BASE_DIR / "resume.txt")
        absolute = Path(tempfile.gettempdir()) / "cv.txt"
        self.assertEqual(config.resolve_project_path(str(absolute)), absolute)

    def test_matcher_consumes_stored_job_description(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            resume = Path(tmp) / "resume.txt"
            resume.write_text("Java developer with Spring Boot, REST APIs and microservices", encoding="utf-8")
            matcher = ResumeMatcher(str(resume))
            jd = parse_card({"url": "https://www.naukri.com/job-listings-x-500000000001", "title": "Java Dev"})
            jd.job_description = "Role & responsibilities\n\n- Build Spring Boot microservices\n- Design REST APIs"
            score = matcher.calculate_match_score(jd.job_description)
            self.assertIsInstance(score, float)
            self.assertGreater(score, 0)
            self.assertLessEqual(score, 100)
            self.assertEqual(matcher.calculate_match_score(""), 0.0)

    def test_matcher_missing_resume_scores_zero(self):
        matcher = ResumeMatcher(str(Path(tempfile.gettempdir()) / "definitely-missing-resume.txt"))
        self.assertEqual(matcher.resume_text, "")
        self.assertEqual(matcher.calculate_match_score("Java developer"), 0.0)


if __name__ == "__main__":
    unittest.main()
