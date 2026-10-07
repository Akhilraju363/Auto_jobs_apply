"""Phase 2 file handoff: export discovered jobs for the AI Agent, import its scores back."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ai_agent_bridge as bridge
import db
from naukri_parser import parse_card

LONG_JD = "Design and build Spring Boot microservices with Angular front ends and REST APIs. " * 3


def make_job(job_id: str, description: str = LONG_JD, **card_overrides) -> dict:
    card = {
        "job_id": job_id,
        "url": f"https://www.naukri.com/job-listings-java-developer-acme-pune-{job_id}?src=srp",
        "title": "Java Developer",
        "company": "Acme",
        "location": "Pune",
        "posted": "1 day ago",
        "tags": ["Java", "Spring Boot", "Microservices", "Angular", "REST APIs"],
    }
    card.update(card_overrides)
    job = parse_card(card)
    job.job_description = description
    return job.to_dict()


def agent_result(job_url: str, score=9, **overrides) -> dict:
    result = {
        "title": "Java Developer", "company": "Acme", "link": job_url, "description": LONG_JD,
        "source": "Naukri", "score": score, "reasoning": "Strong Java and Spring Boot overlap.",
        "matched_must_haves": ["Java", "Spring Boot"], "missing_must_haves": ["Kafka"],
        "qualified": isinstance(score, int) and score >= 8,
    }
    result.update(overrides)
    return result


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite handles linger on Windows
        self.tmp = Path(self._tmp.name)
        patcher = mock.patch.object(db, "DB_PATH", self.tmp / "jobs.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        db.init_db()
        self.export_path = self.tmp / "exports" / "naukri_jobs.json"
        self.scored_path = self.tmp / "agent_output" / "scored_jobs.json"

    def store(self, *jobs: dict) -> None:
        for job in jobs:
            self.assertTrue(db.save_discovered_job(job))

    def export(self, limit=50):
        result = bridge.export_discovered_jobs(path=self.export_path, limit=limit)
        return result, json.loads(self.export_path.read_text(encoding="utf-8"))

    def write_scores(self, entries) -> None:
        self.scored_path.parent.mkdir(parents=True, exist_ok=True)
        self.scored_path.write_text(json.dumps(entries), encoding="utf-8")

    def stored(self, job_id: str) -> dict:
        return next(j for j in db.get_jobs(status=None) if j["job_id"] == job_id)


class TestExport(BridgeTestCase):
    def test_exports_only_discovered_jobs(self):
        self.store(make_job("700000000001"), make_job("700000000002"))
        db.save_job_score(self.stored("700000000001")["id"], 90, "recommended")
        result, records = self.export()
        self.assertEqual(result.discovered, 1)
        self.assertEqual([r["source_job_id"] for r in records], ["700000000002"])

    def test_export_limit_and_stable_order(self):
        self.store(*(make_job(f"70000000001{i}") for i in range(5)))
        result, records = self.export(limit=2)
        self.assertEqual((result.eligible, len(result.exported)), (5, 2))
        self.assertEqual([r["source_job_id"] for r in records], ["700000000010", "700000000011"])
        self.assertEqual(bridge.export_discovered_jobs(path=self.export_path).limit, bridge.AI_AGENT_EXPORT_LIMIT)

    def test_missing_title_or_company_is_skipped(self):
        no_company = make_job("700000000020", company="")
        no_title = dict(make_job("700000000021"), title="")
        self.store(no_company, no_title, make_job("700000000022"))
        result, records = self.export()
        self.assertEqual(len(records), 1)
        self.assertEqual(sorted(reason for _, reason in result.skipped), ["missing company", "missing title"])

    def test_short_description_supplemented_with_real_skills_only_in_payload(self):
        short = "Role & responsibilities\n\nPreferred candidate profile"  # 52 chars, as seen on Naukri
        self.store(make_job("700000000030", description=short))
        result, [record] = self.export()
        self.assertEqual(result.supplemented, 1)
        self.assertEqual(
            record["description"],
            short + "\n\nKey skills:\nJava, Spring Boot, Microservices, Angular, REST APIs",
        )
        self.assertGreaterEqual(bridge.agent_description_length(record["description"]), 80)
        self.assertEqual(self.stored("700000000030")["job_description"], short)  # DB untouched

    def test_short_description_without_enough_skills_is_skipped(self):
        self.store(make_job("700000000031", description="Hiring now", tags=[]),
                   make_job("700000000032", description="Hiring now", tags=["Go"]))
        result, records = self.export()
        self.assertEqual(records, [])
        self.assertEqual(len(result.skipped), 2)
        self.assertTrue(all("shorter than 80" in reason for _, reason in result.skipped))

    def test_record_contract(self):
        self.store(make_job("700000000040"))
        _, [record] = self.export()
        self.assertEqual(
            list(record),
            ["title", "company", "link", "description", "posted_date", "location", "source", "found_at",
             "source_job_id", "dedup_key", "skills"],
        )
        self.assertEqual(record["source"], "Naukri")
        self.assertEqual(record["link"], "https://www.naukri.com/job-listings-java-developer-acme-pune-700000000040")
        self.assertEqual(record["link"], bridge.agent_canonical_link(record["link"] + "?a=1#b"))
        self.assertEqual(record["source_job_id"], "700000000040")
        self.assertEqual(record["dedup_key"], "naukri:id:700000000040")
        self.assertRegex(record["found_at"], r"^\d{4}-\d{2}-\d{2}$")

    def test_export_contains_no_sensitive_or_browser_data(self):
        self.store(make_job("700000000041"))
        self.export()
        text = self.export_path.read_text(encoding="utf-8").lower()
        for marker in ("cookie", "token", "api_key", "password", "chrome_user_data", "session"):
            self.assertNotIn(marker, text)

    def test_canonical_link_matches_agent_rule(self):
        self.assertEqual(bridge.agent_canonical_link("https://www.naukri.com/a-1?x=1#y"), "https://www.naukri.com/a-1")
        self.assertIsNone(bridge.agent_canonical_link(None))


class TestImport(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.store(make_job("800000000001"), make_job("800000000002"), make_job("800000000003"))
        self.urls = {j["job_id"]: j["url"] for j in db.get_jobs()}

    def test_score_mapping_and_labels(self):
        self.write_scores([
            agent_result(self.urls["800000000001"], 9),
            agent_result(self.urls["800000000002"], 7),
            agent_result(self.urls["800000000003"], 3),
        ])
        result = bridge.import_agent_scores(self.scored_path)
        self.assertEqual(result.scored_now, 3)
        self.assertEqual(result.by_status, {"recommended": 1, "review": 1, "skipped": 1})
        job = self.stored("800000000001")
        self.assertEqual((job["status"], job["match_score"], job["match_status"]), ("scored", 90.0, "recommended"))
        self.assertEqual(job["match_reason"], "Strong Java and Spring Boot overlap.")
        self.assertEqual((job["matching_skills"], job["missing_skills"]), (["Java", "Spring Boot"], ["Kafka"]))
        self.assertIsNone(job["experience_match"])
        self.assertIsNone(job["location_match"])
        self.assertIsNotNone(job["scored_at"])
        self.assertEqual(self.stored("800000000002")["match_status"], "review")
        self.assertEqual(self.stored("800000000003")["match_score"], 30.0)

    def test_classify_score_boundaries(self):
        expected = {10: "recommended", 8: "recommended", 7: "review", 6: "review", 5: "skipped", 1: "skipped"}
        self.assertEqual({s: bridge.classify_score(s) for s in expected}, expected)

    def test_malformed_results_are_logged_and_skipped(self):
        self.write_scores([
            agent_result(self.urls["800000000001"], score="high"),
            agent_result(self.urls["800000000002"], score=11),
            agent_result(self.urls["800000000003"], 8, matched_must_haves="Java"),
            "not-an-object",
        ])
        result = bridge.import_agent_scores(self.scored_path)
        self.assertEqual((result.malformed, result.scored_now, result.unmatched), (3, 0, 1))
        self.assertEqual(len(db.get_jobs(status="discovered")), 3)

    def test_second_import_is_idempotent(self):
        self.write_scores([agent_result(self.urls["800000000001"], 9)])
        first = bridge.import_agent_scores(self.scored_path)
        scored_at = self.stored("800000000001")["scored_at"]
        self.write_scores([agent_result(self.urls["800000000001"], 4)])  # a later, different result
        second = bridge.import_agent_scores(self.scored_path)
        self.assertEqual((first.scored_now, second.scored_now, second.already_scored), (1, 0, 1))
        job = self.stored("800000000001")
        self.assertEqual((job["match_score"], job["scored_at"]), (90.0, scored_at))

    def test_matches_by_canonical_url_and_ignores_other_sources(self):
        self.write_scores([
            agent_result(self.urls["800000000001"] + "?trackingId=abc", 8, dedup_key=None),
            agent_result("https://www.linkedin.com/jobs/view/123", 9, source="LinkedIn"),
        ])
        result = bridge.import_agent_scores(self.scored_path)
        self.assertEqual((result.scored_now, result.unmatched), (1, 1))

    def test_dedup_key_fallback_when_url_differs(self):
        self.write_scores([agent_result("https://www.naukri.com/job-listings-renamed-slug-x",
                                        8, dedup_key="naukri:id:800000000002")])
        self.assertEqual(bridge.import_agent_scores(self.scored_path).scored_now, 1)
        self.assertEqual(self.stored("800000000002")["status"], "scored")

    def test_failed_job_stays_discovered_and_is_the_only_one_re_exported(self):
        self.write_scores([agent_result(self.urls["800000000001"], 9), agent_result(self.urls["800000000002"], 6)])
        result = bridge.import_agent_scores(self.scored_path)
        self.assertEqual((result.scored_now, result.still_discovered), (2, 1))
        self.assertEqual(self.stored("800000000003")["status"], "discovered")
        _, records = self.export()
        self.assertEqual([r["source_job_id"] for r in records], ["800000000003"])

    def test_missing_scores_file_changes_nothing(self):
        with self.assertRaises(FileNotFoundError):
            bridge.import_agent_scores(self.tmp / "nope" / "scored_jobs.json")
        self.assertEqual(len(db.get_jobs(status="discovered")), 3)

    def test_unconfigured_output_dir(self):
        with mock.patch.object(bridge, "AI_AGENT_OUTPUT_DIR", None), self.assertRaises(ValueError):
            bridge.import_agent_scores()

    def test_import_never_deletes_rows(self):
        self.write_scores([agent_result(self.urls["800000000001"], 2)])
        bridge.import_agent_scores(self.scored_path)
        with sqlite3.connect(db.DB_PATH) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
