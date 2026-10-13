"""Opt-in auto-apply workflow (application_pipeline auto_apply=True / --workflow auto-apply).

The real ApplicationRunner, eligibility rules, scope files and SQLite history (temporary database) are
used; Naukri pages, the browser and the AI Agent are fakes. Nothing here can submit a real application.
"""

import contextlib
import io
import json
import os
from argparse import Namespace
from pathlib import Path
from unittest import mock

import application_pipeline as pipeline
import db
import naukri_application as app
from ai_agent_bridge import AgentRunResult
from tests.test_application_pipeline import FakeScanner, PipelineBrowser, PipelineTestCase
from tests.test_naukri_application import APPLIED, JOB_URL, snap

AUTO_STAGES = list(pipeline.AUTO_APPLY_STAGES)
FACT_QUESTIONS = [{"index": 0, "type": "text", "label": "Total experience"},
                  {"index": 1, "type": "select", "label": "Notice period", "options": ["30 days", "60 days"]}]


def job_url(naukri_id: str) -> str:
    return JOB_URL.replace("900000000001", naukri_id)


class SiteBrowser(PipelineBrowser):
    """Opening a job URL lands on that job's own page (its Naukri id in the URL), unless `landing` maps
    the URL somewhere else (a redirect). page_kwargs shape the page (captcha, auth, buttons)."""

    def __init__(self, landing=None, page_kwargs=None, **kwargs):
        super().__init__(**kwargs)
        self.landing, self.page_kwargs = dict(landing or {}), dict(page_kwargs or {})

    def open_job(self, url):
        self.opened.append(url)
        self.last = snap(url=self.landing.get(url, url), **self.page_kwargs)
        return self.last


class AutoApplyCase(PipelineTestCase):
    """History already holds one confirmed SAFE-mode application (the AUTO readiness prerequisite)."""

    with_confirmed_history = True

    def setUp(self):
        super().setUp()
        if self.with_confirmed_history:
            earlier = self.add_job("900000000099", scored=False, posted_label="3 days ago")  # stale, never exported
            db.record_application_attempt(earlier, "applied")
        self.browser = SiteBrowser()
        self.make_resumes = True

    def auto_settings(self, **overrides):
        values = dict(mode="AUTO", sheets_enabled=False, export_limit=2, max_applications=1,
                      agent_stages=AUTO_STAGES, score_threshold=8)
        values.update(overrides)
        return self.settings(**values)

    def tailoring_agent(self, agent_dir, stages, export_path, python, timeout, scope_path=None):
        """Scores every exported job 9/10 and (unless disabled) writes its verified resume."""
        exported = json.loads(Path(export_path).read_text(encoding="utf-8"))
        self.agent_calls.append(exported)
        self.agent_scopes.append(scope_path)
        scored = [dict(e, score=9, reasoning="fit", matched_must_haves=["Java"], missing_must_haves=[])
                  for e in exported]
        (self.agent_out / "scored_jobs.json").write_text(json.dumps(scored), encoding="utf-8")
        ids = [e["source_job_id"] for e in exported]
        if self.make_resumes:
            for e in exported:
                self.make_resume(url=job_url(e["source_job_id"]), rid=f"r{e['source_job_id']}")
        self.write_agent_report(scope_path, score={"newly_scored": ids},
                                tailor={"newly_tailored": ids if self.make_resumes else []})
        return AgentRunResult(True, list(stages))

    def run_auto(self, jobs=("900000000001",), settings=None, agent=None, human=(), **flags):
        replies = list(human)

        def prompt(message):
            self.prompts.append(message)
            return replies.pop(0) if replies else ""

        self.pipeline = pipeline.ApplicationPipeline(
            settings or self.auto_settings(), scanner_factory=lambda: FakeScanner(self, jobs=list(jobs)),
            browser_factory=lambda: self.browser, runner_factory=self.make_runner,
            agent_runner=agent or self.tailoring_agent,
            tracker_factory=lambda *a: self.fail("Google Sheets must not be used"),
            prompt=prompt, out=lambda *_: None, auto_apply=True, **flags)
        return self.pipeline.run()

    def outcomes(self, summary):
        return [(r["naukri_job_id"], r["outcome"]) for r in summary.applications or []]


class TestCurrentRunScope(AutoApplyCase):
    def test_confirmed_application_end_to_end(self):
        summary = self.run_auto()
        [record] = summary.applications
        self.assertEqual((record["outcome"], record["state"], record["code"]), ("APPLIED_CONFIRMED", "applied", "APPLIED"))
        self.assertEqual(record["identity_check"]["verified"], True)
        self.assertEqual(record["identity_check"]["expected"], "900000000001")
        self.assertEqual(record["resume"]["version"], 1)
        self.assertEqual((self.browser.clicks, summary.applied, summary.exit_code), (1, 1, 0))
        self.assertTrue(db.has_successful_application(db.get_job(record["job_id"])))
        self.assertEqual(self.prompts, [])  # nobody was asked
        self.assertTrue(summary.scope_validation["ok"])
        self.assertEqual(summary.sheets.status, "DISABLED")
        self.assertEqual(len(db.get_sheet_syncs()), 1)  # the outcome stays queued for a later, separate sync

    def test_historical_recommendations_never_reach_the_runner(self):
        old = self.fresh_job("900000000005", score=95.0)  # recommended, fresh, resume ready: eligible in CLI AUTO
        self.make_resume(url=old["url"], rid="old")
        summary = self.run_auto(settings=self.auto_settings(max_applications=2))
        self.assertEqual(self.browser.opened, [job_url("900000000001")])
        self.assertEqual(self.outcomes(summary), [("900000000001", "APPLIED_CONFIRMED")])
        self.assertEqual(summary.candidates_outside_run, 1)
        self.assertEqual([r["naukri_job_id"] for r in summary.eligibility_results], ["900000000001"])
        untouched = db.get_job(old["id"])
        self.assertIsNone(untouched["application_state"])
        self.assertFalse(db.has_successful_application(untouched))

    def test_nothing_exported_means_nothing_applied_even_with_eligible_history(self):
        old = self.fresh_job("900000000005", score=95.0)
        self.make_resume(url=old["url"], rid="old")
        summary = self.run_auto(jobs=())
        self.assertEqual((self.browser.opened, summary.applications), ([], []))
        self.assertFalse(summary.scope_validation["ok"])
        self.assertEqual(summary.candidates_outside_run, 1)

    def test_tampered_scope_file_blocks_every_application(self):
        def tampering_agent(*args, scope_path=None):
            result = self.tailoring_agent(*args, scope_path=scope_path)
            scope = json.loads(Path(scope_path).read_text(encoding="utf-8"))
            scope["run_id"] = "someone-elses-run"
            Path(scope_path).write_text(json.dumps(scope), encoding="utf-8")
            return result

        summary = self.run_auto(agent=tampering_agent)
        self.assertFalse(summary.scope_validation["ok"])
        self.assertIn("belongs to run", summary.scope_validation["detail"])
        self.assertEqual((self.browser.opened, self.browser.clicks, summary.applications), ([], 0, []))
        self.assertTrue(any("Current-run scope rejected" in w for w in summary.warnings))

    def test_scope_with_different_jobs_blocks_every_application(self):
        def swapping_agent(*args, scope_path=None):
            result = self.tailoring_agent(*args, scope_path=scope_path)
            scope = json.loads(Path(scope_path).read_text(encoding="utf-8"))
            scope["jobs"].append({"id": "900000000777", "link": job_url("900000000777")})
            Path(scope_path).write_text(json.dumps(scope), encoding="utf-8")
            return result

        summary = self.run_auto(agent=swapping_agent)
        self.assertIn("differ", summary.scope_validation["detail"])
        self.assertEqual(self.browser.clicks, 0)

    def test_validate_run_scope_fails_closed(self):
        scope = self.tmp / "scope.json"
        expected = {7: "900000000007"}
        cases = [
            (None, "r1", "nothing was exported"),
            (scope, "r1", "unreadable"),
        ]
        for path, run_id, detail in cases:
            self.assertIn(detail, pipeline.validate_run_scope(path, run_id, expected)["detail"])
        for content, detail in (("{", "unreadable"), ("[]", "not an object"),
                                (json.dumps({"run_id": "r2", "jobs": [{"id": "900000000007"}]}), "belongs to run"),
                                (json.dumps({"run_id": "r1", "jobs": []}), "differ"),
                                (json.dumps({"run_id": "r1", "jobs": [{"id": "900000000007"}, {"id": "8"}]}), "differ")):
            scope.write_text(content, encoding="utf-8")
            result = pipeline.validate_run_scope(scope, "r1", expected)
            self.assertFalse(result["ok"])
            self.assertIn(detail, result["detail"])
        scope.write_text(json.dumps({"run_id": "r1", "jobs": [{"id": "900000000007", "link": "x"}]}), encoding="utf-8")
        self.assertTrue(pipeline.validate_run_scope(scope, "r1", expected)["ok"])
        self.assertFalse(pipeline.validate_run_scope(scope, "r1", {})["ok"])


class TestIdentityAndResume(AutoApplyCase):
    def test_identity_mismatch_prevents_submission(self):
        self.browser = SiteBrowser(landing={job_url("900000000001"): job_url("900000000444")})
        summary = self.run_auto()
        [record] = summary.applications
        self.assertEqual((record["outcome"], record["code"], self.browser.clicks),
                         ("APPLICATION_SKIPPED", app.JOB_IDENTITY_MISMATCH, 0))
        self.assertTrue(record["manual_completion_required"])
        self.assertEqual({k: record["identity_check"][k] for k in ("expected", "found", "verified")},
                         {"expected": "900000000001", "found": "900000000444", "verified": False})
        self.assertIn("900000000444", record["reason"])
        stored = db.get_job(record["job_id"])
        self.assertEqual((stored["application_state"], stored["application_code"]),
                         ("requires_manual_action", app.JOB_IDENTITY_MISMATCH))
        self.assertEqual(db.get_attempts(record["job_id"]), [])  # no attempt was even started
        self.assertEqual(self.browser.opened, [job_url("900000000001")])  # no other listing was tried

    def test_unreadable_job_id_on_the_page_prevents_submission(self):
        self.browser = SiteBrowser(landing={job_url("900000000001"): "https://www.naukri.com/job-listings-java-developer"})
        summary = self.run_auto()
        self.assertEqual((summary.applications[0]["code"], self.browser.clicks), (app.JOB_IDENTITY_MISMATCH, 0))
        self.assertIn("could not be read", summary.applications[0]["reason"])

    def test_identity_is_not_checked_outside_auto_apply(self):
        runner = self.make_runner(self.browser, lambda m: "y", None, False)
        self.assertFalse(runner.require_job_identity)

    def test_missing_resume_prevents_submission(self):
        self.make_resumes = False  # (the base fixture already holds a resume for ...001, so use ...002)
        summary = self.run_auto(jobs=("900000000002",))
        self.assertEqual((self.browser.opened, summary.applications), ([], []))
        [evaluated] = summary.eligibility_results
        self.assertEqual((evaluated["eligible"], evaluated["resume_ready"]), (False, False))

    def test_score_below_threshold_is_not_applied(self):
        def low_scores(agent_dir, stages, export_path, python, timeout, scope_path=None):
            exported = json.loads(Path(export_path).read_text(encoding="utf-8"))
            scored = [dict(e, score=7, reasoning="weak", matched_must_haves=[], missing_must_haves=["Java"])
                      for e in exported]
            (self.agent_out / "scored_jobs.json").write_text(json.dumps(scored), encoding="utf-8")
            return AgentRunResult(True, list(stages))

        summary = self.run_auto(agent=low_scores)
        self.assertEqual((self.browser.opened, summary.applications, summary.eligibility_results), ([], [], []))


class TestFormsAndOutcomes(AutoApplyCase):
    def test_form_filled_from_configured_facts_is_submitted_and_confirmed(self):
        self.browser = SiteBrowser(outcomes=[{"kind": "form", "questions": FACT_QUESTIONS}, {"kind": "applied"}])
        summary = self.run_auto()
        self.assertEqual(self.outcomes(summary), [("900000000001", "APPLIED_CONFIRMED")])
        self.assertEqual((len(self.browser.filled), self.browser.submits), (1, 1))
        self.assertEqual(self.prompts, [])

    def test_unanswerable_question_is_never_guessed_or_submitted(self):
        questions = [{"index": 0, "type": "text", "label": "Why do you want this job?"}]
        self.browser = SiteBrowser(outcomes=[{"kind": "form", "questions": questions}])
        summary = self.run_auto()
        [record] = summary.applications
        # Apply was clicked before the form appeared, so the job is uncertain until reconciled -- never success
        self.assertEqual((record["outcome"], record["code"], self.browser.submits),
                         ("APPLICATION_STATUS_UNKNOWN", app.UNSUPPORTED_QUESTION, 0))
        self.assertEqual(self.browser.filled, [])
        self.assertEqual(self.prompts, [])  # handed off as manual completion, not asked or answered

    def test_submission_success_requires_confirmation_and_is_not_retried(self):
        self.browser = SiteBrowser(outcomes=[{"kind": "unknown"}])
        summary = self.run_auto()
        [record] = summary.applications
        self.assertEqual((record["outcome"], record["state"]), ("APPLICATION_STATUS_UNKNOWN", "recovery_required"))
        self.assertEqual((summary.applied, summary.recovery_required, summary.exit_code), (0, 1, 2))
        job = db.get_job(record["job_id"])
        self.assertFalse(db.has_successful_application(job))
        # next run: the uncertain job is never offered or clicked again
        self.browser = SiteBrowser()
        again = self.run_auto(jobs=())
        self.assertEqual((self.browser.clicks, self.browser.opened, again.applications), (0, [], []))
        self.assertEqual(db.get_job(job["id"])["application_state"], "recovery_required")

    def test_uncertain_outcome_stops_further_applications_this_run(self):
        self.browser = SiteBrowser(outcomes=[{"kind": "unknown"}])
        summary = self.run_auto(jobs=("900000000001", "900000000002"), settings=self.auto_settings(max_applications=2))
        outcomes = [o for _, o in self.outcomes(summary)]
        self.assertEqual(outcomes, ["APPLICATION_STATUS_UNKNOWN", "APPLICATION_SKIPPED"])
        self.assertEqual(summary.applications[1]["code"], "NOT_ATTEMPTED")
        self.assertEqual(self.browser.clicks, 1)

    def test_already_applied_on_naukri_is_skipped_not_clicked(self):
        self.browser = SiteBrowser(page_kwargs={"buttons": APPLIED})
        summary = self.run_auto()
        [record] = summary.applications
        self.assertEqual((record["outcome"], record["code"], self.browser.clicks),
                         ("APPLICATION_SKIPPED", "ALREADY_APPLIED", 0))

    def test_per_run_application_limit(self):
        summary = self.run_auto(jobs=("900000000001", "900000000002"))  # max_applications=1
        outcomes = sorted(o for _, o in self.outcomes(summary))
        self.assertEqual(outcomes, ["APPLICATION_SKIPPED", "APPLIED_CONFIRMED"])
        limited = next(r for r in summary.applications if r["outcome"] == "APPLICATION_SKIPPED")
        self.assertEqual(limited["code"], "PER_RUN_LIMIT")
        self.assertEqual(self.browser.clicks, 1)


class TestInterruptions(AutoApplyCase):
    def test_captcha_pauses_for_the_human_and_q_stops_safely(self):
        self.browser = SiteBrowser(page_kwargs={"captcha": True})
        summary = self.run_auto(jobs=("900000000001", "900000000002"), settings=self.auto_settings(max_applications=2),
                                human=("q",))
        self.assertTrue(any(p.startswith(pipeline.VERIFICATION_PROMPT) for p in self.prompts))  # a real pause
        self.assertEqual([o for _, o in self.outcomes(summary)], ["CAPTCHA_REQUIRED", "APPLICATION_SKIPPED"])
        self.assertEqual(summary.applications[1]["code"], "NOT_ATTEMPTED")
        self.assertEqual(self.browser.clicks, 0)
        self.assertIn("CAPTCHA_REQUIRED", summary.stopped)

    def test_captcha_still_shown_after_the_pause_is_not_applied(self):
        self.browser = SiteBrowser(page_kwargs={"captcha": True})
        summary = self.run_auto(human=("",))  # Enter, but the page still shows the verification
        self.assertEqual((summary.applications[0]["outcome"], self.browser.clicks), ("CAPTCHA_REQUIRED", 0))

    def test_login_required_on_the_job_page_stops_safely(self):
        self.browser = SiteBrowser(page_kwargs={"auth": "no"})
        summary = self.run_auto(jobs=("900000000001", "900000000002"), settings=self.auto_settings(max_applications=2))
        self.assertEqual([o for _, o in self.outcomes(summary)], ["AUTH_REQUIRED", "APPLICATION_SKIPPED"])
        self.assertEqual(self.browser.clicks, 0)

    def test_session_login_required_skips_the_application_step(self):
        self.browser = SiteBrowser(session="login_required")
        summary = self.run_auto()
        self.assertIsNone(summary.applications)
        self.assertEqual((summary.naukri_login, self.browser.clicks), ("AUTH_REQUIRED", 0))


class TestReadinessGate(AutoApplyCase):
    with_confirmed_history = False

    def test_auto_refused_until_one_safe_application_is_confirmed(self):
        summary = self.run_auto()
        self.assertIn("AUTO mode readiness", summary.fatal)
        self.assertEqual((summary.exit_code, self.browser.opened, self.agent_calls), (1, [], []))
        self.assertIsNone(summary.applications)


class TestGuardPromptAndOutcomes(AutoApplyCase):
    def guard(self, args=None, env=None, **settings):
        return pipeline.workflow_guard("auto-apply", args or Namespace(no_apply=False, sync_only=False),
                                       self.auto_settings(**settings), env={"artifact_sync": "0"} if env is None else env,
                                       project_env_keys=set())

    def test_bounded_configuration_passes(self):
        self.assertEqual(self.guard(), [])
        self.assertEqual(self.guard(max_applications=2, score_threshold=9), [])

    def test_every_unsafe_setting_is_refused(self):
        for case in (dict(args=Namespace(no_apply=True, sync_only=False)), dict(args=Namespace(no_apply=False, sync_only=True)),
                     dict(mode="SAFE"), dict(sheets_enabled=True), dict(env={}), dict(env={"artifact_sync": "1"}),
                     dict(export_limit=3), dict(window_hours=48), dict(max_applications=3), dict(max_applications=0),
                     dict(score_threshold=7), dict(agent_stages=["scrape_jobs.py", "score_jobs.py"]),
                     dict(agent_stages=AUTO_STAGES + ["company_research.py"])):
            with self.subTest(case=case):
                self.assertTrue(self.guard(**case))

    def test_scan_and_score_still_refuses_auto_mode(self):
        problems = pipeline.workflow_guard("scan-and-score", Namespace(no_apply=True, sync_only=False),
                                           self.auto_settings(agent_stages=["scrape_jobs.py", "score_jobs.py"],
                                                              max_applications=1),
                                           env={"artifact_sync": "0"}, project_env_keys=set())
        self.assertTrue(any("APPLICATION_MODE must be SAFE" in p for p in problems))

    def test_prompt_policy(self):
        human = []
        answer = pipeline.auto_apply_prompt(lambda m: human.append(m) or "typed", out=lambda *_: None)
        self.assertEqual(answer("Approve this application? [y/n/s/q]: "), "y")
        self.assertEqual(answer("Submit these answers? [y/n]: "), "y")
        self.assertEqual(answer("Press Enter when done, or q to stop: "), "typed")  # the human decides
        self.assertEqual(answer("Press ENTER when done (s = stop for now): "), "s")
        self.assertEqual(answer("Press Enter when you are done (or s to leave it for later): "), "s")
        self.assertEqual(answer("Return this job to the application queue?"), "q")
        self.assertEqual(human, ["Press Enter when done, or q to stop: "])
        cli_auto = pipeline.mode_prompt("AUTO", out=lambda *_: None)  # the CLI AUTO policy is unchanged
        self.assertEqual(cli_auto("Submit these answers? [y/n]: "), "q")

    def test_outcome_labels(self):
        cases = {("applied", "APPLIED"): "APPLIED_CONFIRMED", ("recovery_required", "SUBMISSION_UNCONFIRMED"):
                 "APPLICATION_STATUS_UNKNOWN", ("recovery_required", "CAPTCHA_REQUIRED"): "APPLICATION_STATUS_UNKNOWN",
                 ("requires_manual_action", "CAPTCHA_REQUIRED"): "CAPTCHA_REQUIRED",
                 ("requires_login", "AUTH_REQUIRED"): "AUTH_REQUIRED", ("failed", "APPLICATION_FORM_CHANGED"):
                 "APPLICATION_FAILED", ("already_applied", "ALREADY_APPLIED"): "APPLICATION_SKIPPED",
                 ("external_application", "EXTERNAL_APPLICATION"): "APPLICATION_SKIPPED",
                 ("requires_manual_action", "JOB_IDENTITY_MISMATCH"): "APPLICATION_SKIPPED"}
        for (state, code), label in cases.items():
            self.assertEqual(pipeline.submission_outcome(state, code), label, (state, code))


class TestAutoApplyReport(AutoApplyCase):
    def run_main(self, argv, settings):
        def factory(settings, **kwargs):
            self.factory_kwargs = dict(kwargs)
            kwargs.pop("agent_runner", None)
            return pipeline.ApplicationPipeline(
                settings, scanner_factory=lambda: FakeScanner(self, jobs=["900000000001"]),
                browser_factory=lambda: self.browser, runner_factory=self.make_runner,
                agent_runner=self.tailoring_agent, tracker_factory=lambda *a: self.fail("Sheets"),
                prompt=lambda m: self.fail(f"must not prompt: {m}"), out=print, **kwargs)

        with mock.patch.dict(os.environ, {"artifact_sync": "0"}), \
                mock.patch.object(pipeline, "own_env_keys", return_value=set()), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return pipeline.main(argv, settings=settings, pipeline_factory=factory)

    def test_result_json_records_every_application(self):
        run_dir = self.tmp / "run"
        code = self.run_main(["--workflow", "auto-apply", "--run-dir", str(run_dir)], self.auto_settings())
        report = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertTrue(self.factory_kwargs["auto_apply"])
        self.assertEqual((code, report["workflow"], report["application_mode"], report["no_apply"]),
                         (0, "auto-apply", "AUTO", False))
        self.assertEqual(report["application_outcomes"], {"APPLIED_CONFIRMED": 1})
        [record] = report["applications"]
        self.assertEqual((record["naukri_job_id"], record["outcome"], record["identity_check"]["verified"]),
                         ("900000000001", "APPLIED_CONFIRMED", True))
        self.assertTrue(report["scope_validation"]["ok"])
        self.assertEqual(report["candidates_outside_run"], 0)
        self.assertEqual(report["eligibility_results"][0]["resume_ready"], True)
        self.assertEqual(report["exported_job_ids"], [record["job_id"]])
        self.assertIn("AUTO-APPLY (this run's exported jobs only)", (run_dir / "run.log").read_text(encoding="utf-8"))

    def test_guard_refusal_runs_nothing(self):
        run_dir = self.tmp / "run"
        code = self.run_main(["--no-apply", "--workflow", "auto-apply", "--run-dir", str(run_dir)], self.auto_settings())
        report = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((code, report["status"]), (1, "failed"))
        self.assertIn("cannot be combined with --no-apply", report["fatal"])
        self.assertEqual((self.browser.opened, self.agent_calls), ([], []))

    def test_scan_and_score_report_has_null_application_fields(self):
        run_dir = self.tmp / "run"
        settings = self.settings(sheets_enabled=False, export_limit=2, agent_stages=["scrape_jobs.py", "score_jobs.py"])
        self.run_main(["--no-apply", "--workflow", "scan-and-score", "--run-dir", str(run_dir)], settings)
        report = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertFalse(self.factory_kwargs["auto_apply"])
        for key in ("applications", "application_outcomes", "scope_validation", "eligibility_results",
                    "candidates_outside_run"):
            self.assertIsNone(report[key], key)
