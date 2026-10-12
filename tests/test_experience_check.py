"""Strict experience eligibility against the master resume (experience_check.py).

Pure checks use no database; the storage and pipeline tests use the temporary SQLite database of
the Phase 3 / Phase 4 test cases. Nothing here opens a browser or submits anything.
"""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import db
from eligibility import evaluate_job
from experience_check import (
    ELIGIBLE,
    INELIGIBLE,
    REVIEW_REQUIRED,
    VerifiedExperience,
    check_experience,
    find_requirements,
    verified_resume_experience,
)
from tests.test_application_pipeline import PipelineTestCase

RESUME = VerifiedExperience(4.0, "4+ years", "base_resume.md")
REFERENCE = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


def jd(text: str, **structured) -> dict:
    return dict({"title": "Java Full Stack Developer", "job_description": text}, **structured)


def naukri(low, high, text=None, description="Java, Spring Boot and Angular") -> dict:
    return jd(description, experience_min=low, experience_max=high,
              experience_text=text or f"{low} - {high} years")


class TestExtraction(unittest.TestCase):
    def minimums(self, text):
        return [(r.min_years, r.max_years) for r in find_requirements(text, "description")]

    def test_required_phrases(self):
        cases = {
            "3+ years of experience in Java": [(3, None)],
            "4+ years of experience with Spring Boot": [(4, None)],
            "We need 5+ years experience": [(5, None)],
            "5–8 years of experience in full stack": [(5, 8)],
            "4 to 6 years of experience": [(4, 6)],
            "Minimum 4 years in backend development": [(4, None)],
            "Minimum of 5+ years of professional experience": [(5, None)],
            "at least 3 years with Angular": [(3, None)],
            "Experience: 5-8 Yrs": [(5, 8)],
            "Experience: 5�8 Years": [(5, 8)],  # en dash stored as the replacement character
            "Java Full Stack Developer (5-9yrs exp)": [(5, 9)],
            "Five years of experience in Java": [(5, None)],
            "Experience: 5.00 - 10.00 Years": [(5, 10)],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.minimums(text), expected)

    def test_non_requirements_are_ignored(self):
        for text in (
            "Our company has been in business for 25 years.",  # no experience context
            "Senior Level | 5 - 10 Yrs",  # a table row, not this job's requirement
            "Experience: 511 Years",  # garbled scrape (> 40 years)
            "up to 5 years of experience",  # states no minimum
            "Strong experience with Java and Spring Boot",  # no number at all
        ):
            with self.subTest(text=text):
                self.assertEqual(self.minimums(text), [])

    def test_title_mentions_need_no_context(self):
        self.assertEqual([(r.min_years, r.max_years) for r in
                          find_requirements("Java Full Stack Developer-(5-8 years)", "title", need_context=False)],
                         [(5, 8)])


class TestMatching(unittest.TestCase):
    def test_minimum_at_or_below_verified_years_is_eligible(self):
        for job in (jd("3+ years of experience"), jd("4+ years of experience"), jd("Minimum 4 years of experience"),
                    naukri(2, 5), naukri(0, 0, "Fresher")):
            with self.subTest(job=job.get("experience_text") or job["job_description"]):
                self.assertEqual(check_experience(job, RESUME).status, ELIGIBLE)

    def test_eligible_reason_names_both_sides(self):
        check = check_experience(naukri(3, 5), RESUME)
        self.assertEqual(check.reason, "Experience OK: JD requires 3–5 years; resume supports 4+ years.")
        self.assertEqual((check.required_text, check.required_min, check.verified_text, check.verified_years),
                         ("3–5 years", 3, "4+ years", 4.0))
        self.assertIs(check.match, True)


class TestMismatch(unittest.TestCase):
    def test_minimum_above_verified_years_is_ineligible(self):
        for job in (jd("5+ years of experience"), jd("Minimum 5 years of experience"), naukri(6, 10),
                    jd("4.5 years of experience required")):  # 4+ does not prove 4.5
            with self.subTest(job=job.get("experience_text") or job["job_description"]):
                check = check_experience(job, RESUME)
                self.assertEqual(check.status, INELIGIBLE)
                self.assertIs(check.match, False)

    def test_skip_reason_format(self):
        check = check_experience(naukri(5, 8), RESUME)
        self.assertEqual(check.reason, "Skipped: JD requires 5–8 years; resume supports 4+ years.")
        self.assertEqual(check.required_min, 5)

    def test_naukri_minimum_above_verified_wins_over_lower_jd_text(self):
        job = naukri(5, 10, description="2+ years of experience with Angular")
        self.assertEqual(check_experience(job, RESUME).status, INELIGIBLE)

    def test_experience_is_never_inflated(self):
        self.assertEqual(check_experience(naukri(5, 8), VerifiedExperience(4.0, "4+ years")).status, INELIGIBLE)
        self.assertEqual(check_experience(naukri(4, 8), VerifiedExperience(3.0, "3 years")).status, INELIGIBLE)


class TestRanges(unittest.TestCase):
    def test_ranges_compare_the_minimum_not_the_maximum(self):
        self.assertEqual(check_experience(jd("4 to 6 years of experience"), RESUME).status, ELIGIBLE)
        self.assertEqual(check_experience(naukri(3, 8), RESUME).status, ELIGIBLE)  # max 8 > 4 is fine
        self.assertEqual(check_experience(naukri(1, 3), RESUME).status, ELIGIBLE)  # above the max is fine
        self.assertEqual(check_experience(jd("5–8 years of experience"), RESUME).status, INELIGIBLE)
        self.assertEqual(check_experience(naukri(4, 9), RESUME).status, ELIGIBLE)


class TestMissingAndAmbiguous(unittest.TestCase):
    def test_missing_requirement_needs_review(self):
        check = check_experience(jd("Strong Java and Spring Boot skills."), RESUME)
        self.assertEqual(check.status, REVIEW_REQUIRED)
        self.assertIsNone(check.required_text)
        self.assertIs(check.match, None)
        self.assertIn("no experience requirement found", check.reason)

    def test_ambiguous_requirement_needs_review(self):
        self.assertEqual(check_experience(jd("Several years of experience with Java."), RESUME).status,
                         REVIEW_REQUIRED)
        self.assertEqual(check_experience(jd("Experience: not disclosed", experience_text="Not Disclosed"),
                                          RESUME).status, REVIEW_REQUIRED)

    def test_conflicting_requirements_need_review(self):
        job = naukri(4, 9, description="Minimum of 7+ years of relevant engineering experience")
        check = check_experience(job, RESUME)
        self.assertEqual(check.status, REVIEW_REQUIRED)
        self.assertIn("conflicting", check.reason)
        mixed = jd("5+ years of experience overall, 3+ years of experience in Angular")
        self.assertEqual(check_experience(mixed, RESUME).status, REVIEW_REQUIRED)

    def test_garbled_range_matching_naukri_is_not_a_conflict(self):
        # "3-5 years" scraped as "35 years": exactly Naukri's range with the dash lost
        self.assertEqual(check_experience(naukri(3, 5, description="Developer with 35 years of experience"),
                                          RESUME).status, ELIGIBLE)
        # "24 years" next to Naukri's 2 - 7 is not that range: it stays a conflict for a human
        self.assertEqual(check_experience(naukri(2, 7, description="Developer with 24 years of experience"),
                                          RESUME).status, REVIEW_REQUIRED)

    def test_unverified_resume_needs_review(self):
        check = check_experience(naukri(2, 3), None)
        self.assertEqual(check.status, REVIEW_REQUIRED)
        self.assertIn("master resume", check.reason)


class TestVerifiedResume(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "base_resume.md"

    def verified(self, text, environ=None):
        self.path.write_text(text, encoding="utf-8")
        return verified_resume_experience(self.path, environ or {})

    def test_reads_the_explicit_claim_as_its_floor(self):
        v = self.verified("Software Engineer with 4+ years of experience in full-stack development.")
        self.assertEqual((v.years, v.text), (4.0, "4+ years"))

    def test_lowest_claim_wins_and_dates_are_not_counted(self):
        v = self.verified("6 years of experience in IT. 4+ years of professional experience in Java.")
        self.assertEqual(v.years, 4.0)
        self.assertIsNone(self.verified("Jan 2022 – Jun 2024 Software Engineer"))

    def test_environment_can_lower_but_never_raise(self):
        text = "4+ years of experience"
        self.assertEqual(self.verified(text, {"APPLICANT_TOTAL_EXPERIENCE": "6"}).years, 4.0)
        self.assertEqual(self.verified(text, {"APPLICANT_TOTAL_EXPERIENCE": "3.5"}).years, 3.5)

    def test_missing_file(self):
        self.assertIsNone(verified_resume_experience(Path(self._tmp.name) / "missing.md", {}))
        self.assertIsNone(verified_resume_experience(None, {}))


class TestEvaluateJob(unittest.TestCase):
    BASE = {"posted_label": "5 hours ago", "match_score": 90, "experience_min": 3, "experience_max": 5}

    def evaluate(self, **changes):
        return evaluate_job(dict(self.BASE, **changes), reference_time=REFERENCE, preferred_locations=[],
                            applicant_experience=RESUME, resume_ready=True)

    def test_eligible_and_reasons(self):
        self.assertTrue(self.evaluate().eligible)
        result = self.evaluate(experience_min=5, experience_max=8, experience_text="5 - 8 years")
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "Skipped: JD requires 5–8 years; resume supports 4+ years.")
        self.assertEqual(result.experience_status, INELIGIBLE)

    def test_review_required_is_never_eligible(self):
        result = self.evaluate(experience_min=None, experience_max=None)
        self.assertFalse(result.eligible)
        self.assertEqual(result.experience_status, REVIEW_REQUIRED)

    def test_ai_experience_flag_cannot_override_the_resume(self):
        self.assertFalse(self.evaluate(experience_min=6, experience_max=9, experience_match=True).eligible)

    def test_no_verified_experience_is_never_eligible(self):
        result = evaluate_job(self.BASE, reference_time=REFERENCE, preferred_locations=[], resume_ready=True)
        self.assertFalse(result.eligible)
        self.assertEqual(result.experience_status, REVIEW_REQUIRED)


class TestStorageAndPipeline(PipelineTestCase):
    def stored(self, job):
        return db.get_job(job["id"])

    def test_check_is_stored_on_the_job_row(self):
        job = self.add_job("900000000011", scored=False, experience_min=5, experience_max=8,
                           experience_text="5 - 8 years")
        from experience_check import record_experience_check
        record_experience_check(job, RESUME)
        row = self.stored(job)
        self.assertEqual(row["experience_check_status"], INELIGIBLE)
        self.assertEqual(row["experience_required"], "5–8 years")
        self.assertEqual(row["experience_required_min"], 5)
        self.assertEqual(row["experience_verified"], "4+ years")
        self.assertEqual(row["experience_verified_years"], 4)
        self.assertEqual(row["experience_check_reason"], "Skipped: JD requires 5–8 years; resume supports 4+ years.")
        self.assertIsNotNone(row["experience_checked_at"])

    def test_failed_jobs_are_not_exported_to_the_ai_agent(self):
        """The experience gate runs before export, so no scoring/tailoring call is spent on them."""
        too_senior = self.add_job("900000000021", scored=False, posted_label="2 hours ago",
                                  experience_min=5, experience_max=8, experience_text="5 - 8 years")
        unreadable = self.add_job("900000000022", scored=False, posted_label="2 hours ago",
                                  experience_min=None, experience_max=None, experience_text="")
        fits = self.add_job("900000000023", scored=False, posted_label="2 hours ago")
        lines = []
        p = self.pipeline(out=lines.append)
        summary = p.run()
        self.assertEqual(len(self.agent_calls), 1)
        self.assertEqual(len(self.agent_calls[0]), 1)
        exported = json.dumps(self.agent_calls[0])
        self.assertIn("900000000023", exported)
        self.assertNotIn("900000000021", exported)
        self.assertNotIn("900000000022", exported)
        self.assertEqual((summary.experience_ineligible, summary.experience_review, summary.exported), (1, 1, 1))
        self.assertEqual(self.stored(too_senior)["experience_check_status"], INELIGIBLE)
        self.assertEqual(self.stored(unreadable)["experience_check_status"], REVIEW_REQUIRED)
        self.assertEqual(self.stored(fits)["experience_check_status"], ELIGIBLE)
        self.assertTrue(any("Skipped: JD requires 5–8 years; resume supports 4+ years." in line for line in lines))

    def test_ineligible_candidate_is_never_applied(self):
        job = self.fresh_job(experience_min=5, experience_max=8, experience_text="5 - 8 years")
        summary = self.run_pipeline(answers=("y",))
        self.assertEqual(self.browser.clicks, 0)
        self.assertEqual((summary.eligible, summary.applied, summary.experience_ineligible), (0, 0, 1))
        self.assertEqual(self.stored(job)["experience_check_status"], INELIGIBLE)
        self.assertEqual(self.prompts, [])

    def test_missing_master_resume_blocks_everything(self):
        self.fresh_job()
        summary = self.run_pipeline(answers=("y",), settings=self.settings(master_resume_path=self.tmp / "none.md"))
        self.assertEqual((summary.eligible, summary.applied, summary.experience_review), (0, 0, 1))
        self.assertEqual(self.browser.clicks, 0)

    def test_eligibility_check_report_never_applies(self):
        import application_pipeline as pipeline
        ok = self.fresh_job("900000000031")
        skip = self.fresh_job("900000000032", experience_min=6, experience_max=10, experience_text="6 - 10 years")
        review = self.fresh_job("900000000033", experience_min=None, experience_max=None, experience_text="")
        lines = []
        groups = pipeline.run_eligibility_check(self.settings(), out=lines.append)
        ids = {key: [job["id"] for job, _ in items] for key, items in groups.items()}
        self.assertEqual(ids["SKIPPED"], [skip["id"]])
        self.assertEqual(ids["REVIEW_REQUIRED"], [review["id"]])
        self.assertIn(ok["id"], ids["ELIGIBLE"] + ids["NOT ELIGIBLE (other rule)"])
        self.assertEqual(self.browser.clicks, 0)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.stored(skip)["experience_check_status"], INELIGIBLE)

    def pipeline(self, out):
        import application_pipeline as pipeline
        import sheets_tracker as st
        return pipeline.ApplicationPipeline(
            self.settings(), skip_scan=True, no_apply=True,
            scanner_factory=lambda: None, browser_factory=lambda: self.browser, runner_factory=self.make_runner,
            agent_runner=self.fake_agent, tracker_factory=lambda sheet_id, tab: st.SheetTracker(self.gws, sheet_id, tab),
            prompt=lambda message: "", out=out)


if __name__ == "__main__":
    unittest.main()
