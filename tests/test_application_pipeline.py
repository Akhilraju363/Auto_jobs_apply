"""Phase 4 orchestrator: SAFE / AUTO modes, eligibility, history, Sheets sync, summary, start_local.bat.

The real ApplicationRunner, eligibility rules and SQLite history are used; Naukri (browser), the AI
Agent (subprocess) and Google Sheets (gws) are fakes. Nothing here can submit a real application.
"""

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import application_pipeline as pipeline
import db
import naukri_application as app
import sheets_tracker as st
from ai_agent_bridge import AgentRunResult
from naukri_parser import parse_card
from naukri_scanner import ScanResult
from tests.test_naukri_application import ANSWERS, JOB_URL, FakeBrowser, Phase3TestCase, snap
from tests.test_phase4_persistence import FakeGws

ROOT = Path(__file__).resolve().parent.parent


class PipelineBrowser(FakeBrowser):
    """FakeBrowser as a context manager with a session check."""

    def __init__(self, session="authenticated", **kwargs):
        super().__init__(**kwargs)
        self.session = session

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def session_state(self):
        return self.session


class FakeScanner:
    def __init__(self, test, jobs=()):
        self.test, self.jobs, self.closed = test, jobs, False

    def scan(self):
        for job_id in self.jobs:
            self.test.add_job(job_id, scored=False, posted_label="2 hours ago")
        return ScanResult(keywords=["Java"], pages_scanned=1, cards_found=len(self.jobs) + 1,
                          unique_jobs=len(self.jobs) + 1, new_jobs_stored=len(self.jobs), duplicates_skipped=1)

    def close(self):
        self.closed = True


class PipelineTestCase(Phase3TestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.tmp / "chrome_user_data"
        self.profile.mkdir()
        (self.profile / "Local State").write_text("{}", encoding="utf-8")
        self.browser = PipelineBrowser()
        self.gws = FakeGws()
        self.prompts = []
        self.agent_calls = []

    def settings(self, **overrides):
        values = dict(mode="SAFE", window_hours=24, score_threshold=8, preferred_locations=[], max_applications=5,
                      export_limit=5, export_path=self.tmp / "exports" / "naukri_jobs.json", agent_dir=None,
                      agent_output_dir=self.agent_out, agent_python=None, agent_stages=["score_jobs.py"],
                      sheets_enabled=True, sheet_id="sheet-id", sheet_tab="Sheet1", chrome_profile=self.profile,
                      naukri_enabled=True)
        values.update(overrides)
        return pipeline.PipelineSettings(**values)

    def fresh_job(self, job_id="900000000001", score=90.0, match_status="recommended", **extra):
        extra.setdefault("posted_label", "2 hours ago")
        return self.add_job(job_id, score, match_status, **extra)

    def make_runner(self, browser, prompt, out, dry_run):
        runner = app.ApplicationRunner(browser, prompt=prompt, out=lambda *_: None, dry_run=dry_run, answers=ANSWERS,
                                       agent_output_dir=self.agent_out)
        runner.debug_dir, runner.confirmation_wait = self.tmp / "debug", 0
        return runner

    def run_pipeline(self, answers=("y",), settings=None, gws=None, scanner=None, agent=None, **flags):
        replies = list(answers)

        def prompt(message):
            self.prompts.append(message)
            return replies.pop(0) if replies else ""

        flags.setdefault("skip_scan", scanner is None)
        p = pipeline.ApplicationPipeline(
            settings or self.settings(),
            scanner_factory=lambda: scanner,
            browser_factory=lambda: self.browser,
            runner_factory=self.make_runner,
            agent_runner=agent or self.fake_agent,
            tracker_factory=lambda sheet_id, tab: st.SheetTracker(gws or self.gws, sheet_id, tab),
            prompt=prompt, out=lambda *_: None, **flags)
        return p.run()

    def fake_agent(self, agent_dir, stages, export_path, python, timeout):
        self.agent_calls.append(json.loads(Path(export_path).read_text(encoding="utf-8")))
        return AgentRunResult(True, list(stages))


class TestSafeMode(PipelineTestCase):
    def test_explicit_yes_applies_verifies_persists_and_tracks(self):
        job = self.fresh_job()
        summary = self.run_pipeline(answers=("y",))
        self.assertEqual(self.browser.clicks, 1)
        self.assertEqual((summary.eligible, summary.applied, summary.failed), (1, 1, 0))
        self.assertEqual(db.get_job(job["id"])["application_state"], "applied")
        self.assertTrue(db.has_successful_application(job))
        self.assertEqual((summary.sheets.status, summary.sheets.synced, summary.sheets.pending), ("SYNCED", 1, 0))
        self.assertEqual(dict(zip(st.HEADERS, self.gws.rows[1]))["Status"], "Applied")
        self.assertTrue(self.prompts[0].startswith(pipeline.APPROVAL_PROMPT))
        self.assertEqual(summary.exit_code, pipeline.EXIT_OK)

    def test_yes_word_is_accepted(self):
        self.fresh_job()
        self.assertEqual(self.run_pipeline(answers=("yes",)).applied, 1)

    def test_anything_else_is_not_approval(self):
        for answer in ("n", "", "a", "all", "approve all", "ok", "Y es"):
            with self.subTest(answer=answer):
                job = self.fresh_job()
                db.set_application_state(job["id"], "ready", None, "reset")
                self.browser = PipelineBrowser()
                summary = self.run_pipeline(answers=(answer,))
                self.assertEqual(self.browser.clicks, 0)
                self.assertEqual(summary.applied, 0)
                self.assertFalse(db.has_successful_application(job))

    def test_successfully_applied_job_is_never_resubmitted(self):
        job = self.fresh_job()
        self.run_pipeline(answers=("y",))
        self.browser = PipelineBrowser()
        summary = self.run_pipeline(answers=("y", "y"))
        self.assertEqual(self.browser.clicks, 0)
        self.assertEqual((summary.eligible, summary.applied, summary.already_applied), (0, 0, 1))
        self.assertEqual(db.get_job(job["id"])["application_state"], "applied")


class TestAutoMode(PipelineTestCase):
    def test_auto_refused_until_one_confirmed_application(self):
        self.fresh_job()
        summary = self.run_pipeline(settings=self.settings(mode="AUTO"))
        self.assertEqual(summary.exit_code, pipeline.EXIT_CONFIG)
        self.assertIn("AUTO mode readiness", summary.fatal)
        self.assertEqual(self.browser.clicks, 0)

    def test_auto_applies_eligible_job_without_prompt(self):
        first = self.fresh_job()
        self.run_pipeline(answers=("y",))  # the verified SAFE-mode application
        self.assertTrue(db.has_successful_application(first))
        # a second, different job of the same company
        url = JOB_URL.replace("900000000001", "900000000002")
        self.make_resume(url=url, rid="def456")
        second = self.fresh_job("900000000002")
        self.browser = PipelineBrowser()
        self.prompts.clear()
        summary = self.run_pipeline(answers=(), settings=self.settings(mode="AUTO"))
        self.assertEqual((self.browser.clicks, summary.applied), (1, 1))
        self.assertEqual(self.prompts, [])  # nobody was asked
        self.assertTrue(db.has_successful_application(second))

    def test_auto_answers_only_the_review_approval(self):
        answer = pipeline.mode_prompt("AUTO", out=lambda *_: None)
        self.assertEqual(answer("Approve this application? [y/n/s/q]: "), "y")
        self.assertEqual(answer("Submit these answers? [y/n]: "), "q")
        self.assertEqual(answer("Press ENTER when done (s = stop for now): "), "s")
        self.assertEqual(answer("Press Enter when you are done (or s to leave it for later): "), "s")
        self.assertEqual(answer("Press Enter when done, or q to stop: "), "q")
        safe = pipeline.mode_prompt("SAFE", prompt=lambda m: "typed")
        self.assertEqual(safe("Approve this application? "), "typed")

    def test_invalid_mode_is_an_error_not_auto(self):
        checks = pipeline.validate_configuration(self.settings(mode="FAST"))
        self.assertFalse(pipeline.configuration_ok(checks))


class TestEligibilityIntegration(PipelineTestCase):
    def test_score_threshold(self):
        self.fresh_job(score=70, match_status="review")
        summary = self.run_pipeline()
        self.assertEqual((summary.candidates, summary.eligible, self.browser.clicks), (0, 0, 0))

    def test_stricter_configured_threshold(self):
        self.fresh_job(score=80)
        summary = self.run_pipeline(settings=self.settings(score_threshold=9))
        self.assertEqual((summary.candidates, summary.eligible, self.browser.clicks), (1, 0, 0))

    def test_stale_or_ambiguous_jobs_are_not_applied(self):
        for label in ("3 days ago", "1 day ago", ""):
            with self.subTest(label=label):
                job = self.fresh_job(posted_label=label)
                summary = self.run_pipeline()
                self.assertEqual((summary.eligible, self.browser.clicks), (0, 0))
                with db.sqlite3.connect(db.DB_PATH) as conn:
                    conn.execute("DELETE FROM jobs WHERE id = ?", (job["id"],))

    def test_recovery_required_job_is_not_resubmitted(self):
        job = self.fresh_job()
        attempt = db.start_attempt(job, "apply")
        db.mark_apply_clicked(attempt)  # a crash after the click
        summary = self.run_pipeline(answers=("y",))
        self.assertEqual((summary.eligible, self.browser.clicks), (0, 0))

    def test_unverified_resume_blocks(self):
        self.fresh_job()
        self.make_resume(ok=False)  # the AI Agent's validation of this artifact failed
        summary = self.run_pipeline()
        self.assertEqual((summary.eligible, self.browser.clicks), (0, 0))


class TestOrchestrator(PipelineTestCase):
    def test_full_flow_scan_export_score_import_apply_track(self):
        scanner = FakeScanner(self, jobs=["900000000001"])

        def agent(agent_dir, stages, export_path, python, timeout):
            exported = json.loads(Path(export_path).read_text(encoding="utf-8"))
            self.agent_calls.append(exported)
            scored = [dict(e, score=9, reasoning="strong fit", matched_must_haves=["Java"], missing_must_haves=[])
                      for e in exported]
            (self.agent_out / "scored_jobs.json").write_text(json.dumps(scored), encoding="utf-8")
            return AgentRunResult(True, list(stages))

        summary = self.run_pipeline(answers=("y",), scanner=scanner, agent=agent)
        self.assertTrue(scanner.closed)
        self.assertEqual((summary.jobs_scanned, summary.new_jobs, summary.duplicates_removed), (2, 1, 1))
        self.assertEqual((summary.fresh_jobs, summary.exported, summary.agent_status), (1, 1, "OK"))
        self.assertEqual([e["link"] for e in self.agent_calls[0]], [JOB_URL])
        self.assertEqual((summary.scores_imported, summary.recommended, summary.eligible), (1, 1, 1))
        self.assertEqual((summary.applied, summary.sheets.synced, summary.exit_code), (1, 1, 0))
        text = pipeline.format_summary(summary)
        for line in ("NAUKRI AUTO JOB APPLICATION", "Naukri login:", "AI SCORING", "ELIGIBILITY", "APPLICATIONS",
                     "GOOGLE SHEETS", "RUN COMPLETE"):
            self.assertIn(line, text)

    def test_only_fresh_unapplied_jobs_are_exported(self):
        self.add_job("900000000001", scored=False, posted_label="2 hours ago")
        self.add_job("900000000002", scored=False, posted_label="3 days ago")
        applied = self.add_job("900000000003", scored=False, posted_label="1 hour ago")
        db.record_application_attempt(applied, "applied")
        summary = self.run_pipeline(no_apply=True)
        self.assertEqual((summary.fresh_jobs, summary.already_applied, summary.exported), (2, 1, 1))
        self.assertEqual([e["source_job_id"] for e in self.agent_calls[0]], ["900000000001"])

    def test_sheets_failure_never_causes_reapplication(self):
        job = self.fresh_job()
        summary = self.run_pipeline(answers=("y",), gws=FakeGws(fail=st.AUTH_REQUIRED))
        self.assertEqual((summary.applied, summary.sheets.status, summary.sheets.pending), (1, "AUTH_REQUIRED", 1))
        self.assertEqual(db.get_job(job["id"])["application_state"], "applied")
        self.browser = PipelineBrowser()
        summary = self.run_pipeline(answers=("y",))  # next run: Sheets works again
        self.assertEqual((self.browser.clicks, summary.applied), (0, 0))
        self.assertEqual((summary.sheets.synced, summary.sheets.pending), (1, 0))

    def test_sync_only_retries_pending_rows(self):
        job = self.fresh_job()
        db.set_application_state(job["id"], "applied", "APPLIED")
        db.record_application_attempt(job, "applied")
        db.queue_sheet_sync(job["id"])
        summary = self.run_pipeline(sync_only=True)
        self.assertEqual((summary.sheets.synced, self.browser.clicks, summary.naukri_login), (1, 0, "SKIPPED"))

    def test_login_required_skips_applications(self):
        self.fresh_job()
        self.browser = PipelineBrowser(session="login_required")
        summary = self.run_pipeline(answers=("y",))
        self.assertEqual((summary.naukri_login, self.browser.clicks, summary.exit_code), ("AUTH_REQUIRED", 0, 2))

    def test_dry_run_clicks_and_writes_nothing(self):
        job = self.fresh_job()
        summary = self.run_pipeline(answers=("y",), dry_run=True)
        self.assertEqual((self.browser.clicks, summary.dry_run_ready, summary.applied), (0, 1, 0))
        self.assertEqual(db.get_sheet_syncs(), [])
        self.assertEqual(self.gws.calls, [])
        self.assertEqual(db.get_attempts(job["id"]), [])

    def test_agent_failure_is_reported_and_the_run_continues(self):
        self.add_job("900000000001", scored=False, posted_label="2 hours ago")
        failing = lambda *a: AgentRunResult(False, failed_stage="score_jobs.py", code="AGENT_STAGE_FAILED",  # noqa: E731
                                            message="score_jobs.py exited with code 1")
        summary = self.run_pipeline(agent=failing)
        self.assertIn("AGENT_STAGE_FAILED", summary.agent_status)
        self.assertEqual(summary.exit_code, pipeline.EXIT_ATTENTION)

    def test_sheets_disabled_keeps_outcomes_queued(self):
        self.fresh_job()
        summary = self.run_pipeline(answers=("y",), settings=self.settings(sheets_enabled=False))
        self.assertEqual((summary.applied, summary.sheets.status, summary.sheets.pending), (1, "DISABLED", 1))
        self.assertEqual(self.gws.calls, [])

    def test_max_applications_per_run(self):
        self.fresh_job()
        summary = self.run_pipeline(answers=("y",), settings=self.settings(max_applications=0))
        self.assertEqual((summary.eligible, self.browser.clicks), (1, 0))


class TestConfiguration(PipelineTestCase):
    def test_validation(self):
        checks = {c.name: c for c in pipeline.validate_configuration(self.settings())}
        self.assertTrue(pipeline.configuration_ok(list(checks.values())))
        self.assertFalse(checks["AI Agent folder"].ok)
        self.assertFalse(checks["AI Agent folder"].fatal)  # scoring is skipped, nothing breaks
        missing = pipeline.validate_configuration(self.settings(chrome_profile=self.tmp / "nope"))
        self.assertFalse(pipeline.configuration_ok(missing))
        self.assertFalse((self.tmp / "nope").exists())  # never created
        bad = pipeline.validate_configuration(self.settings(score_threshold=11))
        self.assertFalse(pipeline.configuration_ok(bad))

    def test_default_mode_is_safe(self):
        import importlib
        import os
        from unittest import mock

        import config
        try:
            with mock.patch.dict(os.environ, {"APPLICATION_MODE": ""}):  # also blocks a value from .env
                self.assertEqual(importlib.reload(config).APPLICATION_MODE, "SAFE")
            with mock.patch.dict(os.environ, {"APPLICATION_MODE": "auto"}):
                self.assertEqual(importlib.reload(config).APPLICATION_MODE, "AUTO")
        finally:
            importlib.reload(config)

    def test_start_local_bat(self):
        bat = (ROOT / "start_local.bat").read_bytes()
        self.assertIn(b"\r\n", bat)
        text = bat.decode("ascii")
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIn(r".venv\Scripts\activate.bat", text)
        self.assertIn("application_pipeline.py --check-config", text)
        self.assertIn("application_pipeline.py %*", text)
        self.assertIn("exit /b %EXITCODE%", text)
        commands = [line.strip().lower() for line in text.splitlines()
                    if line.strip() and not line.strip().lower().startswith(("rem", "echo"))]
        self.assertFalse([c for c in commands if "pip install" in c or "playwright install" in c])
        self.assertFalse([c for c in commands if "chrome_user_data" in c])


if __name__ == "__main__":
    unittest.main()
