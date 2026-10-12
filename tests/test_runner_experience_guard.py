"""The experience guard inside ApplicationRunner.process and the legacy apply loops.

Direct runner calls, bypassing the pipeline and main.py, must still refuse jobs that need more
experience than the master resume supports (4+ years here) or whose requirement is unreadable.
Browser, Naukri and submission are fakes; nothing here can submit a real application.
"""

import sqlite3
from unittest import mock

import db
import naukri_application as app
from application_outcome import OUTCOME_SKIPPED, application_outcome_label
from experience_check import (
    EXPERIENCE_INELIGIBLE,
    EXPERIENCE_REVIEW_REQUIRED,
    INELIGIBLE,
    REVIEW_REQUIRED,
    VerifiedExperience,
)
from tests.test_naukri_application import FakeBrowser, Phase3TestCase

NO_STRUCTURED = dict(experience_min=None, experience_max=None, experience_text="")


class GuardTestCase(Phase3TestCase):
    def job(self, job_id="900000000001", description=None, **extra):
        if description is not None:
            extra["job_description"] = description
        return self.add_job(job_id, 90.0, "recommended", **extra)

    def process(self, job, answers=("y",), dry_run=False, **runner_options):
        self.browser, self.prompts = FakeBrowser(), []
        replies = list(answers)

        def prompt(message):
            self.prompts.append(message)
            return replies.pop(0) if replies else ""

        runner = app.ApplicationRunner(self.browser, prompt=prompt, out=lambda *_: None, dry_run=dry_run,
                                       answers={"total_experience": "4"}, agent_output_dir=self.agent_out,
                                       **runner_options)
        runner.debug_dir, runner.confirmation_wait = self.tmp / "debug", 0
        return runner.process(job)

    def attempts(self, job):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute("SELECT COUNT(*) FROM application_attempts WHERE job_row_id = ?",
                                (job["id"],)).fetchone()[0]

    def assert_blocked(self, job, result, code):
        self.assertEqual((result.state, result.code, result.submitted), ("not_ready", code, False))
        # nothing was opened, asked, clicked or submitted, and no history was written
        self.assertEqual((self.browser.opened, self.browser.clicks, self.browser.submits), ([], 0, 0))
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.applied_rows(), [])
        self.assertEqual(self.attempts(job), 0)
        self.assertFalse(db.has_successful_application(job))
        self.assertEqual(application_outcome_label(result.state, result.code), OUTCOME_SKIPPED)


class TestRunnerPassesEligibleJobs(GuardTestCase):
    def test_3_plus_years_passes_and_applies_after_approval(self):
        job = self.job(description="3+ years of experience in Java", **NO_STRUCTURED)
        result = self.process(job, ("y",))
        self.assertEqual((result.state, result.submitted, self.browser.clicks), ("applied", True, 1))
        self.assertEqual(db.get_job(job["id"])["experience_check_status"], "ELIGIBLE")

    def test_4_plus_years_passes(self):
        job = self.job(description="4+ years of experience in Java", **NO_STRUCTURED)
        self.assertEqual(self.process(job, ("y",)).state, "applied")

    def test_existing_eligible_job_still_applies(self):
        job = self.job()  # Naukri: 3 - 5 years
        result = self.process(job, ("y",))
        self.assertEqual((result.state, self.browser.clicks), ("applied", 1))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])

    def test_safe_mode_still_needs_explicit_approval(self):
        job = self.job()
        result = self.process(job, ("n",))
        self.assertEqual((result.state, result.code, self.browser.clicks), ("ready", app.NOT_APPROVED, 0))
        self.assertTrue(self.prompts[0].startswith("Approve this application?"))


class TestRunnerBlocksIneligibleJobs(GuardTestCase):
    def test_5_plus_years_is_blocked(self):
        job = self.job(description="5+ years of experience in Java", **NO_STRUCTURED)
        result = self.process(job, ("y", "y", "y"))
        self.assert_blocked(job, result, EXPERIENCE_INELIGIBLE)
        self.assertEqual(result.reason, "Skipped: JD requires 5+ years; resume supports 4+ years.")

    def test_5_to_8_years_is_blocked_and_recorded(self):
        job = self.job(experience_min=5, experience_max=8, experience_text="5 - 8 years")
        result = self.process(job, ("y",))
        self.assert_blocked(job, result, EXPERIENCE_INELIGIBLE)
        self.assertEqual(result.reason, "Skipped: JD requires 5–8 years; resume supports 4+ years.")
        row = db.get_job(job["id"])
        self.assertEqual((row["application_state"], row["application_code"]), ("not_ready", EXPERIENCE_INELIGIBLE))
        self.assertEqual((row["experience_check_status"], row["experience_required"]), (INELIGIBLE, "5–8 years"))
        self.assertEqual(row["application_reason"], result.reason)

    def test_conflicting_naukri_and_jd_requirements_need_review(self):
        job = self.job(experience_min=4, experience_max=9, experience_text="4 - 9 years",
                       description="Minimum of 7+ years of relevant engineering experience")
        result = self.process(job, ("y",))
        self.assert_blocked(job, result, EXPERIENCE_REVIEW_REQUIRED)
        self.assertIn("conflicting", result.reason)
        self.assertEqual(db.get_job(job["id"])["experience_check_status"], REVIEW_REQUIRED)

    def test_missing_or_malformed_requirements_need_review(self):
        cases = {
            "missing": dict(NO_STRUCTURED, description="Strong Java and Angular skills."),
            "not disclosed": dict(NO_STRUCTURED, experience_text="Not Disclosed", description="Java developer"),
            "garbled": dict(NO_STRUCTURED, description="Experience: 511 Years"),
            "vague": dict(NO_STRUCTURED, description="Several years of experience with Java."),
        }
        for n, (name, extra) in enumerate(cases.items()):
            with self.subTest(name):
                job = self.job(f"90000000020{n}", **extra)
                self.assert_blocked(job, self.process(job, ("y",)), EXPERIENCE_REVIEW_REQUIRED)

    def test_caller_cannot_loosen_the_stored_requirement(self):
        job = self.job(experience_min=5, experience_max=8, experience_text="5 - 8 years")
        edited = dict(job, experience_min=0, experience_max=4, experience_text="0 - 4 years")
        self.assert_blocked(job, self.process(edited, ("y",)), EXPERIENCE_INELIGIBLE)

    def test_unverified_resume_fails_closed(self):
        job = self.job()
        self.assert_blocked(job, self.process(job, ("y",), verified_experience=None), EXPERIENCE_REVIEW_REQUIRED)

    def test_experience_is_never_inflated(self):
        job = self.job()  # 3 - 5 years
        result = self.process(job, ("y",), verified_experience=VerifiedExperience(2.0, "2 years"))
        self.assert_blocked(job, result, EXPERIENCE_INELIGIBLE)

    def test_an_error_in_the_check_fails_closed(self):
        job = self.job()
        with mock.patch.object(app, "check_experience", side_effect=RuntimeError("boom")):
            result = self.process(job, ("y",))
        self.assert_blocked(job, result, EXPERIENCE_REVIEW_REQUIRED)

    def test_dry_run_blocks_without_writing(self):
        job = self.job(experience_min=6, experience_max=10, experience_text="6 - 10 years")
        result = self.process(job, ("y",), dry_run=True)
        self.assert_blocked(job, result, EXPERIENCE_INELIGIBLE)
        row = db.get_job(job["id"])
        self.assertIsNone(row["application_state"])
        self.assertIsNone(row["experience_check_status"])


class TestExistingProtectionsKeepPrecedence(GuardTestCase):
    def test_already_applied_job_stays_already_applied(self):
        job = self.job()
        self.assertEqual(self.process(job, ("y",)).state, "applied")
        with sqlite3.connect(db.DB_PATH) as conn:  # the posting's requirement is later read as 5 - 8
            conn.execute("UPDATE jobs SET experience_min = 5, experience_max = 8 WHERE id = ?", (job["id"],))
        again = self.process(db.get_job(job["id"]), ("y",))
        self.assertEqual((again.state, self.browser.clicks), ("already_applied", 0))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])
        self.assertEqual(db.get_job(job["id"])["application_state"], "already_applied")

    def test_guard_never_overwrites_final_or_recovery_states(self):
        for state in ("external_application", "recovery_required"):
            with self.subTest(state):
                job = self.job(experience_min=5, experience_max=8, experience_text="5 - 8 years")
                db.set_application_state(job["id"], state, "X", "kept")
                runner = app.ApplicationRunner(FakeBrowser(), prompt=lambda _: "y", out=lambda *_: None,
                                               answers={}, agent_output_dir=self.agent_out)
                blocked = runner.experience_gate(db.get_job(job["id"]))
                self.assertEqual(blocked.code, EXPERIENCE_INELIGIBLE)
                self.assertEqual(db.get_job(job["id"])["application_state"], state)

    def test_stale_job_is_still_not_a_pipeline_candidate(self):
        from eligibility import evaluate_job
        from datetime import datetime, timezone
        job = dict(self.job(), posted_label="3 days ago")
        result = evaluate_job(job, reference_time=datetime.now(timezone.utc), preferred_locations=[],
                              applicant_experience=VerifiedExperience(4.0, "4+ years"))
        self.assertFalse(result.eligible)
        self.assertEqual(result.experience_status, "ELIGIBLE")


class TestLegacyApplyLoops(Phase3TestCase):
    """`main.py --mode cli` drivers click Apply themselves; the same check runs before that."""

    def run_driver(self, module_name, experience):
        import importlib
        module = importlib.import_module(module_name)
        with mock.patch.object(module, "ResumeMatcher"), mock.patch.object(module, "GeminiEngine"), \
                mock.patch.object(module, "HITLEngine"), \
                mock.patch.object(module, "verified_resume_experience",
                                  return_value=VerifiedExperience(4.0, "4+ years")), \
                mock.patch.object(module, "get_applied_jobs_count", return_value=0), \
                mock.patch.object(module, "is_job_applied", return_value=False), \
                mock.patch.object(module.time, "sleep"):
            driver = (module.NaukriDriver if module_name == "naukri_driver" else module.LinkedInDriver)()
            driver.page = object()
            driver.matcher.calculate_match_score.return_value = 100
            job = dict({"id": "x1", "title": "Java Developer", "company": "Acme", "url": "https://x"}, **experience)
            with mock.patch.object(driver, "_search_keyword"), \
                    mock.patch.object(driver, "_get_job_cards", return_value=[object()]), \
                    mock.patch.object(driver, "_extract_job_data", return_value=job), \
                    mock.patch.object(driver, "_get_job_description", return_value=experience.get("jd", "")), \
                    mock.patch.object(driver, "_try_apply_job", return_value=(False, False)) as apply:
                results = driver.search_and_apply_jobs(keywords=["Java"], max_limit=5)
        return results, apply

    def test_legacy_drivers_skip_ineligible_and_unreadable_jobs(self):
        for module_name in ("naukri_driver", "linkedin_driver"):
            for experience in ({"experience_min": 5, "experience_max": 8}, {"jd": "Java and Angular"}):
                with self.subTest(module=module_name, experience=experience):
                    results, apply = self.run_driver(module_name, experience)
                    apply.assert_not_called()
                    self.assertEqual(results["skipped"], 1)

    def test_legacy_drivers_still_try_eligible_jobs(self):
        for module_name in ("naukri_driver", "linkedin_driver"):
            with self.subTest(module=module_name):
                _, apply = self.run_driver(module_name, {"experience_min": 3, "experience_max": 5,
                                                         "jd": "3-5 years of experience"})
                apply.assert_called_once()
