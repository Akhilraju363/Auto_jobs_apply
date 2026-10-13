"""Relevance-first export order (export_priority.py + ai_agent_bridge.export_discovered_jobs(order_key=...)).

Reproduces run 20261013T103118-03c417: with AI_AGENT_EXPORT_LIMIT=2 and jobs taken oldest-first, a
'Junior Developer' and an 'API Testing' job were exported while Java Full Stack / Java Developer jobs were
deferred. Temporary database only; the AI Agent is a fake. Nothing is scanned, scored or applied.
"""

import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ai_agent_bridge as bridge
import application_pipeline as pipeline
import db
import export_priority as ep
from tests.test_application_pipeline import PipelineTestCase

NOW = datetime(2026, 10, 13, 5, 0, tzinfo=timezone.utc)
DESCRIPTION = "Design, build and maintain production services with a team of engineers. " * 3


def job(job_id=1, title="Java Developer", skills=(), description=DESCRIPTION, **extra):
    return dict(id=job_id, title=title, skills=list(skills), job_description=description, **extra)


class TestRoleTier(unittest.TestCase):
    def test_target_roles_and_unrelated_titles(self):
        cases = {
            "Java Full Stack Developer(React.js/Angular.js)": 4,
            "Acentra Health|| Job Opportunity || Java Fullstack": 4,
            "Java Spring Boot Developer": 4,
            "Java Backend Developer": 4,
            "Senior Java Back-End Engineer": 4,
            "Java Developer - Bangalore GS": 3,
            "Software Developer(Java Developer)": 3,
            "JAVA Developer Fresher": 3,
            "Full Stack Developer": 2,
            "Full-Stack Engineer": 2,
            "Full Stack .Net Developer-S": 0,
            "React Native Full-Stack Developer": 0,
            "API Testing": 0,
            "Java API Tester": 0,
            "Selenium Java Automation Testing": 0,
            "JavaScript Developer": 0,
            "Data Engineer": 0,
            "Python GenAi": 0,
            "Technical Lead": 0,
        }
        for title, tier in cases.items():
            with self.subTest(title=title):
                self.assertEqual(ep.role_tier(job(title=title)), tier)

    def test_generic_developer_title_counts_only_with_java_skills(self):
        self.assertEqual(ep.role_tier(job(title="Junior Developer", skills=["Angular", "java"])), 1)
        self.assertEqual(ep.role_tier(job(title="Software Engineer", skills=["Python"], description="Go and C++ " * 10)), 0)
        self.assertEqual(ep.role_tier(job(title="Software Engineer", skills=["Javascript"],
                                          description="JavaScript only " * 10)), 0)

    def test_a_skill_repeated_across_title_skills_and_description_counts_once(self):
        repeated = job(title="Java Spring Boot Developer", skills=["Java", "JAVA", "Spring Boot", "spring boot"],
                       description="Java Java Java and Spring Boot work for a payments team. " * 3)
        self.assertEqual(ep.matching_skills(repeated), ("Java", "Spring Boot", "Spring"))

    def test_java_script_spelled_apart_is_not_java(self):
        self.assertEqual(ep.role_tier(job(title="Java Script Developer")), 0)
        self.assertEqual(ep.matching_skills(job(title="Frontend", skills=["Java Script", "JavaScript"],
                                                description="Java Script and JavaScript UI work. " * 3)), ())

    def test_matching_skills_are_distinct_and_never_javascript(self):
        found = ep.matching_skills(job(title="Java Full Stack", skills=["Java", "Spring Boot", "Microservices",
                                                                         "React.js", "Postgresql", "Javascript"]))
        self.assertEqual(found, ("Java", "Spring Boot", "Spring", "Microservices", "React", "SQL"))
        self.assertEqual(ep.matching_skills(job(title="Frontend", skills=["Javascript", "React Native"],
                                                description="x" * 90)), ())


class TestOrder(unittest.TestCase):
    def key(self):
        return ep.relevance_order(NOW, 24)

    def aged(self, job_id, hours, **kwargs):
        """A job read 'N hours ago' at scrape time NOW (age == hours)."""
        return job(job_id, posted_label=f"{hours} hours ago", scraped_at=NOW.strftime("%Y-%m-%dT%H:%M:%S"), **kwargs)

    def test_relevance_beats_age(self):
        old_unrelated = self.aged(1, 23, title="API Testing", skills=["Postman", "Rest Assured"])
        new_java = self.aged(2, 1, title="Java Full Stack Developer", skills=["Java", "Spring Boot"])
        self.assertEqual([j["id"] for j in sorted([old_unrelated, new_java], key=self.key())], [2, 1])

    def test_more_matching_skills_first_within_a_tier(self):
        few = self.aged(1, 5, title="Java Developer", skills=["Java"])
        many = self.aged(2, 5, title="Java Developer", skills=["Java", "Spring Boot", "Microservices", "SQL"])
        self.assertEqual([j["id"] for j in sorted([few, many], key=self.key())], [2, 1])

    def test_expiry_risk_then_id_break_ties(self):
        fresher = self.aged(1, 2, title="Java Developer", skills=["Java"])
        older = self.aged(2, 20, title="Java Developer", skills=["Java"])
        unknown_age = job(3, title="Java Developer", skills=["Java"], posted_label="1 day ago")
        same_a = self.aged(5, 20, title="Java Developer", skills=["Java"])
        self.assertEqual([j["id"] for j in sorted([unknown_age, fresher, same_a, older], key=self.key())],
                         [2, 5, 1, 3])

    def test_order_is_deterministic(self):
        jobs = [self.aged(i, i % 7 + 1, title=title, skills=skills) for i, (title, skills) in enumerate([
            ("Java Developer", ["Java"]), ("Java Developer", ["Java"]), ("Full Stack Developer", ["React"]),
            ("API Testing", []), ("Java Backend Developer", ["Java", "Kafka"]), ("Java Developer", ["Java"]),
            ("Junior Developer", ["java"]), ("Data Engineer", ["SQL"])], start=10)]
        expected = [j["id"] for j in sorted(jobs, key=self.key())]
        rng = random.Random(7)
        for _ in range(20):
            shuffled = jobs[:]
            rng.shuffle(shuffled)
            self.assertEqual([j["id"] for j in sorted(shuffled, key=self.key())], expected)
        self.assertEqual(expected[0], 14)  # the Java Backend job


class ExportCase(PipelineTestCase):
    def add(self, naukri_id, title, skills, posted="2 hours ago", company="Acme"):
        url = f"https://www.naukri.com/job-listings-x-{naukri_id}"
        return self.add_job(naukri_id, scored=False, url=url, title=title, company=company, skills=list(skills),
                            posted_label=posted, job_description=DESCRIPTION)

    def incident_jobs(self):
        """Inserted in the run's order: the unrelated jobs got the lower (older) row ids."""
        return [self.add("100000000389", "Junior Developer", ["Angular", "java", "HTML"]),
                self.add("100000000390", "API Testing", ["Rest Assured", "API Testing", "Postman"]),
                self.add("100000000400", "Full Stack .Net Developer-S", ["C#", ".NET", "Angular"]),
                self.add("100000000418", "Java Full Stack Developer(React.js/Angular.js)",
                         ["Java", "Spring Boot", "Microservices", "SQL", "React.js", "Hibernate"]),
                self.add("100000000419", "Software Developer(Java Developer)", ["Java", "MySQL"]),
                self.add("100000000442", "Java Full Stack Developer-Senior Associate", ["Java", "React.js"])]


class TestExportSelection(ExportCase):
    def test_default_order_is_unchanged_oldest_first(self):
        jobs = self.incident_jobs()
        result = bridge.export_discovered_jobs(self.tmp / "export.json", 2)
        self.assertEqual(result.exported_ids, [jobs[0]["id"], jobs[1]["id"]])
        self.assertIn("oldest first", result.deferred[0][1])

    def test_relevant_java_jobs_are_exported_ahead_of_unrelated_roles(self):
        jobs = self.incident_jobs()
        result = bridge.export_discovered_jobs(self.tmp / "export.json", 2,
                                               order_key=ep.relevance_order(datetime.now(timezone.utc), 24),
                                               order_label=pipeline.EXPORT_ORDER_LABEL)
        by_naukri = {j["id"]: j["job_id"] for j in jobs}
        self.assertEqual([by_naukri[i] for i in result.exported_ids], ["100000000418", "100000000442"])
        self.assertEqual(len(result.exported), 2)  # the cap is unchanged
        self.assertEqual(len(result.deferred), 4)
        self.assertIn("ranked by role relevance", result.deferred[0][1])
        scope = json.loads(result.scope_path.read_text(encoding="utf-8"))
        self.assertEqual([j["id"] for j in scope["jobs"]], ["100000000418", "100000000442"])
        self.assertEqual(scope["run_id"], result.run_id)

    def test_order_never_changes_which_jobs_qualify(self):
        self.incident_jobs()
        short = self.add("100000000500", "Java Spring Boot Developer", ["Java"])
        db_job = db.get_job(short["id"])
        with db.sqlite3.connect(db.DB_PATH) as conn:  # a record the Agent cannot take (description too short)
            conn.execute("UPDATE jobs SET job_description = ?, skills = '[]' WHERE id = ?", ("short", db_job["id"]))
        result = bridge.export_discovered_jobs(self.tmp / "export.json", 2,
                                               order_key=ep.relevance_order(datetime.now(timezone.utc), 24))
        self.assertIn(short["id"], [job_id for job_id, decision, _ in result.decisions if decision == "REJECTED"])
        self.assertNotIn(short["id"], result.exported_ids)


class TestPipelineExport(ExportCase):
    def test_pipeline_exports_the_java_jobs_and_keeps_the_cap(self):
        lines = []
        self.incident_jobs()
        settings = self.settings(export_limit=2, agent_stages=["scrape_jobs.py", "score_jobs.py"], sheets_enabled=False)
        summary = pipeline.ApplicationPipeline(
            settings, scanner_factory=lambda: None, browser_factory=lambda: self.browser,
            runner_factory=self.make_runner, agent_runner=self.fake_agent, prompt=lambda m: self.fail(m),
            out=lines.append, skip_scan=True, no_apply=True).run()
        exported = [record["source_job_id"] for record in self.agent_calls[0]]
        self.assertEqual(exported, ["100000000418", "100000000442"])
        self.assertEqual((summary.exported, summary.export_deferred, summary.export_limit), (2, 4, 2))
        text = "\n".join(lines)
        # Java, Spring Boot, Spring, Microservices, React, SQL, Hibernate + Angular from the title = 8
        self.assertIn("EXPORTED Java Full Stack Developer(React.js/Angular.js) @ Acme [Java specialization, "
                      "8 matching skill(s)", text)
        self.assertIn("DEFERRED API Testing @ Acme [other role", text)
        self.assertIn("ranked by role relevance, then matching skills, then expiry risk", text)

    def test_experience_ineligible_and_review_required_jobs_never_enter_the_export(self):
        lines = []
        ineligible = self.add("100000000700", "Java Spring Boot Developer", ["Java", "Spring Boot", "Microservices"])
        review = self.add("100000000701", "Java Backend Developer", ["Java", "Spring Boot", "Kafka"])
        eligible = self.add("100000000702", "API Testing", ["Postman"])
        with db.sqlite3.connect(db.DB_PATH) as conn:  # 8-12 years: INELIGIBLE; no requirement at all: REVIEW_REQUIRED
            conn.execute("UPDATE jobs SET experience_min = 8, experience_max = 12, experience_text = '8 - 12 years' "
                         "WHERE id = ?", (ineligible["id"],))
            conn.execute("UPDATE jobs SET experience_min = NULL, experience_max = NULL, experience_text = NULL "
                         "WHERE id = ?", (review["id"],))
        settings = self.settings(export_limit=2, agent_stages=["scrape_jobs.py", "score_jobs.py"], sheets_enabled=False)
        summary = pipeline.ApplicationPipeline(
            settings, scanner_factory=lambda: None, browser_factory=lambda: self.browser,
            runner_factory=self.make_runner, agent_runner=self.fake_agent, prompt=lambda m: self.fail(m),
            out=lines.append, skip_scan=True, no_apply=True).run()
        self.assertEqual([r["source_job_id"] for r in self.agent_calls[0]], ["100000000702"])
        self.assertEqual((summary.export_experience_ineligible, summary.export_experience_review), (1, 1))
        self.assertEqual(summary.exported_ids, [eligible["id"]])
        for blocked in (ineligible, review):
            self.assertEqual(db.get_job(blocked["id"])["status"], "discovered")  # kept, never scored

    def test_main_export_to_agent_keeps_the_default_row_id_order(self):
        import contextlib
        import io
        from unittest import mock

        import main
        fake = mock.MagicMock(discovered=0, eligible=0, supplemented=0, skipped=[], exported=[], deferred=[],
                              path=self.tmp / "export.json")
        with mock.patch.object(main, "export_discovered_jobs", return_value=fake) as export, \
                mock.patch.object(main, "verified_resume_experience", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            main.run_export_to_agent(2)
        _, kwargs = export.call_args
        self.assertNotIn("order_key", kwargs)  # relevance-first ordering is opt-in at the pipeline call site only
        self.assertEqual(kwargs["limit"], 2)

    def test_stale_jobs_are_still_never_exported(self):
        self.add("100000000600", "Java Spring Boot Developer", ["Java", "Spring Boot"], posted="3 days ago")
        self.add("100000000601", "API Testing", ["Postman"])
        settings = self.settings(export_limit=2, agent_stages=["scrape_jobs.py", "score_jobs.py"], sheets_enabled=False)
        summary = pipeline.ApplicationPipeline(
            settings, scanner_factory=lambda: None, browser_factory=lambda: self.browser,
            runner_factory=self.make_runner, agent_runner=self.fake_agent, prompt=lambda m: self.fail(m),
            out=lambda *_: None, skip_scan=True, no_apply=True).run()
        self.assertEqual([r["source_job_id"] for r in self.agent_calls[0]], ["100000000601"])
        self.assertEqual(summary.not_fresh_unscored, 1)


if __name__ == "__main__":
    unittest.main()
