"""Phase 3.3: confirmation, idempotency, interrupted applications, recovery, attempt history,
diagnostics and event privacy. Fake browsers only; never a real Naukri account."""

import contextlib
import io
import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import application_audit as audit
import db
import main
import naukri_application as app
from application_outcome import (
    INTERRUPTED_AFTER_APPLY,
    INTERRUPTED_BEFORE_APPLY,
    RECOVERED_NOT_APPLIED,
    SUBMISSION_UNCONFIRMED,
    decide_idempotency,
    detect_application_confirmation,
)
from tests.test_naukri_application import APPLIED, EASY, JOB_URL, FakeBrowser, Phase3TestCase, snap

SECRET_CTC = "17.25"
PROFILE = {"expected_ctc": SECRET_CTC, "phone": "9876543210", "email": "me@example.com"}


class TestConfirmation(unittest.TestCase):
    def test_explicit_signals_confirm(self):
        for body in ("You have successfully applied to Java Developer", "Applied successfully!",
                     "Your application has been submitted"):
            result = detect_application_confirmation(snap(body=body, buttons=EASY))
            self.assertEqual((result.confirmed, result.signal), (True, "success_message"), body)
        result = detect_application_confirmation(snap(buttons=APPLIED))
        self.assertEqual((result.confirmed, result.signal), (True, "applied_state"))

    def test_missing_or_ambiguous_signals_never_confirm(self):
        ambiguous = {
            "no signal": snap(body="Job description"),
            "url changed, submit gone": snap(url="https://www.naukri.com/mnjuser/homepage", buttons=[], body="Recommended jobs"),
            "weak nav text": snap(body="Jobs you have applied to (12)"),
            "Applied nav link outside the job header": snap(buttons=[{"id": "", "text": "Applied", "in_job_header": False}]),
        }
        for name, snapshot in ambiguous.items():
            result = detect_application_confirmation(snapshot)
            self.assertFalse(result.confirmed, name)
            self.assertIsNone(result.signal, name)

    def test_failure_message(self):
        result = detect_application_confirmation(snap(body="Something went wrong. Please try again."))
        self.assertEqual((result.confirmed, result.failed), (False, True))


class TestIdempotencyDecision(unittest.TestCase):
    JOB = {"id": 1, "application_state": None, "application_code": None}

    def test_rules(self):
        decide = decide_idempotency
        self.assertEqual(decide(self.JOB, True, None, []).state, "already_applied")
        self.assertTrue(decide(self.JOB, False, None, []).proceed)
        stale = decide(self.JOB, False, None, [{"id": 7, "apply_clicked": 0}])
        self.assertEqual((stale.proceed, stale.close_open_attempts), (True, ((7, "interrupted", INTERRUPTED_BEFORE_APPLY),)))
        clicked = decide(self.JOB, False, None, [{"id": 8, "apply_clicked": 1}])
        self.assertEqual((clicked.proceed, clicked.state, clicked.code), (False, "recovery_required", INTERRUPTED_AFTER_APPLY))
        for status in ("unconfirmed", "incomplete", "submission_failed"):
            self.assertEqual(decide(self.JOB, False, {"status": status}, []).state, "recovery_required", status)
        self.assertTrue(decide(self.JOB, False, {"status": "not_applied"}, []).proceed)
        legacy = dict(self.JOB, application_state="requires_manual_action", application_code="UNSUPPORTED_QUESTION")
        self.assertEqual(decide(legacy, False, None, []).state, "recovery_required")
        pre_apply = dict(self.JOB, application_state="failed", application_code="APPLICATION_FORM_CHANGED")
        self.assertTrue(decide(pre_apply, False, None, []).proceed)
        self.assertFalse(decide(dict(self.JOB, application_state="recovery_required"), False, None, []).proceed)


class CrashingBrowser(FakeBrowser):
    def __init__(self, crash_on=None, error=Exception("browser closed"), open_failures=0, **kwargs):
        super().__init__(**kwargs)
        self.crash_on, self.error, self.open_failures, self.open_calls = crash_on, error, open_failures, 0

    def open_job(self, url):
        self.open_calls += 1
        if self.open_calls <= self.open_failures:
            raise TimeoutError("navigation timeout")
        return super().open_job(url)

    def click_easy_apply(self):
        if self.crash_on == "click":
            raise self.error
        super().click_easy_apply()

    def wait_for_outcome(self):
        if self.crash_on == "outcome":
            raise self.error
        return super().wait_for_outcome()


class RecoveryTestCase(Phase3TestCase):
    def setUp(self):
        super().setUp()
        self.debug = self.tmp / "debug"
        self.job = self.add_job()

    def runner(self, browser, answers=("y",), dry_run=False, profile=None):
        runner = super().runner(browser, answers, dry_run, PROFILE if profile is None else profile)
        runner.debug_dir, runner.confirmation_wait = self.debug, 0
        return runner

    def fresh(self):
        return db.get_job(self.job["id"])

    def attempts(self):
        return db.get_attempts(self.job["id"])

    def apply_until_unconfirmed(self):
        result = self.runner(FakeBrowser(outcomes=[{"kind": "unknown"}])).process(self.fresh())
        self.assertEqual((result.state, result.code), ("recovery_required", SUBMISSION_UNCONFIRMED))
        return result


class TestInterruptedApplications(RecoveryTestCase):
    def test_successful_application_records_history(self):
        browser = FakeBrowser(outcomes=[{"kind": "applied", "signal": "success_message"}])
        result = self.runner(browser).process(self.job)
        self.assertEqual((result.state, result.confirmation_signal, browser.clicks), ("applied", "success_message", 1))
        [attempt] = self.attempts()
        self.assertEqual((attempt["kind"], attempt["apply_clicked"], attempt["success"], attempt["state"]), ("apply", 1, 1, "applied"))
        self.assertEqual((attempt["confirmation_signal"], attempt["resume_version"]), ("success_message", 1))
        self.assertTrue(attempt["resume_path"].endswith("v1.md"))
        self.assertIsNotNone(attempt["completed_at"])
        self.assertIsNotNone(attempt["duration_ms"])
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])

    def test_local_history_prevents_duplicate(self):
        self.runner(FakeBrowser()).process(self.job)
        browser = FakeBrowser()
        result = self.runner(browser).process(self.fresh())
        self.assertEqual((result.state, browser.clicks, browser.opened), ("already_applied", 0, []))
        self.assertEqual(len(self.attempts()), 1)

    def test_naukri_already_applied_state(self):
        browser = FakeBrowser(pages=[snap(buttons=APPLIED)])
        result = self.runner(browser).process(self.job)
        self.assertEqual((result.state, browser.clicks, self.attempts()), ("already_applied", 0, []))

    def test_crash_before_apply_click_is_not_a_submission(self):
        browser = CrashingBrowser(crash_on="click", error=RuntimeError("expected exactly one #apply-button, found 2"))
        result = self.runner(browser).process(self.job)
        self.assertEqual((result.state, result.code), ("failed", app.APPLICATION_FORM_CHANGED))
        [attempt] = self.attempts()
        self.assertEqual((attempt["apply_clicked"], attempt["success"]), (0, 0))
        self.assertTrue(decide_idempotency(self.fresh(), False, None, db.open_attempts(self.job["id"])).proceed)

    def test_crash_or_timeout_after_apply_needs_recovery(self):
        for crash_on, error in (("outcome", Exception("Target page, context or browser has been closed")),
                                ("outcome", TimeoutError("network timeout")),
                                ("click", app.ApplyClickUncertain("Apply click did not complete cleanly"))):
            job = self.add_job(str(900000000100 + len(self.attempts()) + (1 if crash_on == "click" else 0) + id(error) % 50))
            self.make_resume(url=job["url"], rid=f"r{job['id']}")
            result = self.runner(CrashingBrowser(crash_on=crash_on, error=error)).process(job)
            self.assertEqual((result.state, result.code, result.submitted), ("recovery_required", SUBMISSION_UNCONFIRMED, False))
            [attempt] = db.get_attempts(job["id"])
            self.assertEqual((attempt["apply_clicked"], attempt["success"], attempt["state"]), (1, 0, "recovery_required"))
            self.assertIn("may have occurred", db.get_job(job["id"])["application_reason"])

    def test_run_killed_after_click_is_detected_next_time(self):
        attempt = db.start_attempt(self.job, "apply")
        db.mark_apply_clicked(attempt)  # the process died here (no completion was ever written)
        browser = FakeBrowser()
        result = self.runner(browser).process(self.fresh())
        self.assertEqual((result.state, result.code, browser.clicks, browser.opened), ("recovery_required", INTERRUPTED_AFTER_APPLY, 0, []))
        self.assertEqual(self.attempts()[0]["state"], "recovery_required")

    def test_run_killed_before_click_is_closed_and_job_continues(self):
        db.start_attempt(self.job, "apply")  # approved, then the process died before the click
        browser = FakeBrowser()
        result = self.runner(browser, ["y"]).process(self.fresh())
        self.assertEqual((result.state, browser.clicks), ("applied", 1))
        first, second = self.attempts()
        self.assertEqual((first["state"], first["code"], first["apply_clicked"]), ("interrupted", INTERRUPTED_BEFORE_APPLY, 0))
        self.assertEqual(second["success"], 1)

    def test_network_timeout_before_submit_is_retried_then_stops(self):
        browser = CrashingBrowser(open_failures=5)
        runner = self.runner(browser)
        runner.navigation_retries = 2
        result = runner.process(self.job)
        self.assertEqual((browser.open_calls, browser.clicks, result.code), (3, 0, app.NETWORK_TIMEOUT))
        self.assertEqual(self.attempts(), [])
        recovered = CrashingBrowser(open_failures=1)
        runner = self.runner(recovered)
        runner.navigation_retries = 2
        self.assertEqual(runner.process(self.fresh()).state, "applied")
        self.assertEqual(recovered.open_calls, 2)

    def test_captcha_and_auth_never_start_an_attempt(self):
        with self.assertRaises(app.ApplicationStop):
            self.runner(FakeBrowser(pages=[snap(captcha=True)]), ["q"]).process(self.job)
        with self.assertRaises(app.ApplicationStop):
            self.runner(FakeBrowser(pages=[snap(auth="no")])).process(self.fresh())
        self.assertEqual(self.attempts(), [])

    def test_ambiguous_confirmation_stops_and_is_never_retried(self):
        self.apply_until_unconfirmed()
        self.assertEqual(self.applied_rows(), [("900000000001", "unconfirmed")])
        for _ in range(2):  # running apply again never clicks
            browser = FakeBrowser()
            result = self.runner(browser).process(self.fresh())
            self.assertEqual((result.state, browser.clicks, browser.opened), ("recovery_required", 0, []))
        self.assertEqual(db.get_application_candidates(), [])
        self.assertEqual(sum(a["apply_clicked"] for a in self.attempts()), 1)

    def test_delayed_confirmation_is_accepted(self):
        browser = FakeBrowser(outcomes=[{"kind": "unknown"}])
        browser.confirmation = app.ConfirmationResult(True, "applied_state", "Naukri shows the job as Applied")
        result = self.runner(browser).process(self.job)
        self.assertEqual((result.state, result.confirmation_signal, browser.awaited), ("applied", "applied_state", 1))

    def test_explicit_failure_message_still_needs_recovery(self):
        browser = FakeBrowser(outcomes=[{"kind": "failed", "reason": "Naukri reported 'something went wrong'"}])
        browser.confirmation = app.ConfirmationResult(False, None, "Naukri reported 'something went wrong'", failed=True)
        result = self.runner(browser).process(self.job)
        self.assertEqual((result.state, result.code), ("recovery_required", app.SUBMISSION_FAILED))
        self.assertEqual(self.applied_rows(), [("900000000001", "submission_failed")])

    def test_successful_attempt_is_immutable(self):
        self.runner(FakeBrowser()).process(self.job)
        [attempt] = self.attempts()
        self.assertFalse(db.finish_attempt(attempt["id"], "failed", "X", "overwrite"))
        db.mark_apply_clicked(attempt["id"], False)
        self.assertEqual(self.attempts(), [attempt])
        self.assertFalse(db.record_application_attempt(self.fresh(), "unconfirmed"))
        self.assertFalse(db.resolve_unconfirmed_record(self.fresh(), "recovery"))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])


class TestRecovery(RecoveryTestCase):
    def test_recovery_sees_applied(self):
        self.apply_until_unconfirmed()
        browser = FakeBrowser(pages=[snap(buttons=APPLIED)])
        result = self.runner(browser).recover(self.fresh())
        self.assertEqual((result.state, result.code, browser.clicks), ("already_applied", "CONFIRMED_DURING_RECOVERY", 0))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])
        self.assertEqual(self.attempts()[-1]["kind"], "recovery")
        self.assertEqual(db.get_application_candidates(), [])

    def test_recovery_sees_available_job_and_requeues_only_with_yes(self):
        self.apply_until_unconfirmed()
        declined = self.runner(FakeBrowser(), ["n"]).recover(self.fresh())
        self.assertEqual(declined.state, "recovery_required")
        result = self.runner(FakeBrowser(), ["y"]).recover(self.fresh())
        self.assertEqual((result.state, result.code), ("ready", RECOVERED_NOT_APPLIED))
        self.assertEqual(self.applied_rows(), [("900000000001", "not_applied")])
        self.assertEqual([j["id"] for j in db.get_application_candidates()], [self.job["id"]])
        # a fresh explicit approval is still required: "n" never clicks
        browser = FakeBrowser()
        self.assertEqual(self.runner(browser, ["n"]).process(self.fresh()).state, "ready")
        self.assertEqual(browser.clicks, 0)
        browser = FakeBrowser()
        self.assertEqual(self.runner(browser, ["y"]).process(self.fresh()).state, "applied")
        self.assertEqual((browser.clicks, self.applied_rows()), (1, [("900000000001", "applied")]))

    def test_recovery_refuses_ambiguous_states(self):
        self.apply_until_unconfirmed()
        for page in (snap(buttons=[]), snap(buttons=EASY + [{"id": "company-site-button", "text": "Apply on company site"}])):
            browser = FakeBrowser(pages=[page])
            result = self.runner(browser, ["y"]).recover(self.fresh())
            self.assertEqual((result.state, browser.clicks), ("recovery_required", 0))
        with self.assertRaises(app.ApplicationStop):
            self.runner(FakeBrowser(pages=[snap(auth="no")])).recover(self.fresh())
        self.assertEqual(self.fresh()["application_state"], "recovery_required")

    def test_recovery_never_clicks_or_answers(self):
        self.apply_until_unconfirmed()
        browser = FakeBrowser(outcomes=[{"kind": "chatbot"}])
        self.runner(browser, ["y"]).recover(self.fresh())
        self.assertEqual((browser.clicks, browser.answered, browser.submits), (0, [], 0))

    def test_resume_artifact_stays_job_specific_in_recovery(self):
        self.apply_until_unconfirmed()
        result = self.runner(FakeBrowser(pages=[snap(buttons=APPLIED)])).recover(self.fresh())
        self.assertEqual(result.resume.resume_id, "abc123")
        self.assertEqual(result.resume.path.name, "v1.md")


class TestDryRunAndIdentity(RecoveryTestCase):
    def test_dry_run_never_submits_or_records(self):
        outputs = []
        runner = self.runner(FakeBrowser(), ["y"], dry_run=True)
        runner.out = outputs.append
        result = runner.process(self.job)
        text = "\n".join(outputs)
        for expected in ("Idempotency:", "Recovery status: not required", "Question handling:",
                         "Submission requires your explicit 'y' approval: YES"):
            self.assertIn(expected, text)
        self.assertEqual((result.state, self.attempts(), self.applied_rows()), ("ready", [], []))
        self.assertIsNone(self.fresh()["application_state"])

    def test_dry_run_reports_blocked_job_without_writing(self):
        self.apply_until_unconfirmed()
        before = (self.fresh()["application_updated_at"], len(self.attempts()))
        outputs = []
        runner = self.runner(FakeBrowser(), ["y"], dry_run=True)
        runner.out = outputs.append
        browser = runner.browser
        self.assertEqual(runner.process(self.fresh()).state, "recovery_required")
        self.assertIn("blocked by idempotency check", "\n".join(outputs))
        self.assertEqual((browser.opened, browser.clicks), ([], 0))
        self.assertEqual((self.fresh()["application_updated_at"], len(self.attempts())), before)

    def test_duplicate_naukri_job_id_is_one_application(self):
        self.runner(FakeBrowser()).process(self.job)
        slug_changed = dict(self.job, id=999, url=JOB_URL.replace("infosys", "infosys-ltd"))
        self.assertTrue(db.has_successful_application(slug_changed))  # identity = Naukri job id
        same_id = dict(db.get_jobs(status=None)[0])
        self.assertFalse(db.save_discovered_job(dict(same_id, url=slug_changed["url"], dedup_key="naukri:url:x")))
        self.assertEqual(len(db.get_jobs(status=None)), 1)

    def test_canonical_url_in_events(self):
        with self.assertLogs("application_events", level=logging.INFO) as logs:
            self.runner(FakeBrowser()).process(dict(self.job, url=JOB_URL + "?src=jobsearch&sid=abc"))
        self.assertTrue(all("sid=abc" not in line for line in logs.output))
        self.assertIn(f"url={JOB_URL}", logs.output[0])


class TestPrivacy(RecoveryTestCase):
    def test_diagnostics_are_sanitized(self):
        page = snap(body="Phone 9876543210 email me@example.com expected 17.25 LPA", title="Infosys | me@example.com",
                    buttons=EASY + [{"id": "x", "text": f"CTC {SECRET_CTC}"}], url=JOB_URL + "?token=abc")
        browser = FakeBrowser(pages=[page], outcomes=[{"kind": "unknown"}])
        result = self.runner(browser).process(self.job)
        record = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
        text = json.dumps(record)
        for secret in (SECRET_CTC, "9876543210", "me@example.com", "token=abc"):
            self.assertNotIn(secret, text)
        self.assertNotIn("body", record["page"])
        self.assertEqual((record["state"], record["code"]), ("recovery_required", SUBMISSION_UNCONFIRMED))
        self.assertEqual(record["resume"]["resume_id"], "abc123")
        self.assertTrue(str(result.diagnostics_path).startswith(str(self.debug)))

    def test_public_job_identifiers_are_not_redacted(self):
        record = audit.build_diagnostics(dict(self.job, url=JOB_URL + "?sid=1"), "failed", "X", "phone 9876543210",
                                         {"url": JOB_URL, "header_controls": ["Continue application"]}, secrets=[SECRET_CTC])
        self.assertEqual((record["job"]["naukri_id"], record["job"]["url"], record["page"]["url"]),
                         ("900000000001", JOB_URL, JOB_URL))
        self.assertEqual(record["page"]["header_controls"], ["Continue application"])
        self.assertNotIn("9876543210", record["reason"])

    def test_redaction_helpers(self):
        text = audit.redact("call +91 98765 43210 or me@x.co, CTC 12 LPA, value 17.25", [SECRET_CTC, "true"])
        for secret in ("98765", "me@x.co", "12 LPA", "17.25"):
            self.assertNotIn(secret, text)
        self.assertEqual(audit.redact("state=true id=12345", ["true"]), "state=true id=12345")
        self.assertIsNone(audit.write_diagnostics(self.job, {}, self.debug, enabled=False))
        with self.assertRaises(ValueError):
            audit.log_event("NOT_AN_EVENT", self.job)

    def test_event_logs_have_no_sensitive_values(self):
        ctc = app.parse_chat({"present": True, "roots": 1, "question": "Expected CTC?", "message_count": 2,
                              "inputs": [{"ref": "input-0", "kind": "contenteditable"}], "options": [],
                              "checkbox_count": 0, "saves": [{"ref": "save-0", "text": "Save"}], "skip_count": 0})
        browser = FakeBrowser(outcomes=[{"kind": "chatbot"}], chat=[ctc, app.ChatState("applied", signature="applied")])
        with self.assertLogs(level=logging.INFO) as logs:
            self.runner(browser).process(self.job)
        text = "\n".join(logs.output)
        for event in ("APPLICATION_START", "JOB_OPENED", "REVIEW_SHOWN", "APPROVAL_RECEIVED", "APPLY_CLICKED",
                      "CONFIRMATION_DETECTED", "APPLICATION_CONFIRMED"):
            self.assertIn(event, text)
        for secret in (SECRET_CTC, "9876543210", "me@example.com"):
            self.assertNotIn(secret, text)


class FakeBrowserCM:
    def __init__(self, browser):
        self.browser = browser

    def __enter__(self):
        return self.browser

    def __exit__(self, *exc):
        return False


class TestCli(RecoveryTestCase):
    def run_cli(self, func, *args, browser=None, answer="y"):
        out = io.StringIO()
        patches = [mock.patch.object(main, "AI_AGENT_OUTPUT_DIR", self.agent_out), mock.patch("builtins.input", return_value=answer)]
        if browser is not None:
            patches.append(mock.patch.object(main, "NaukriApplicationBrowser", lambda: FakeBrowserCM(browser)))
        with contextlib.ExitStack() as stack, contextlib.redirect_stdout(out):
            for p in patches:
                stack.enter_context(p)
            with mock.patch.object(app.ApplicationRunner, "__init__", self._runner_init()):
                code = func(*args)
        return code, out.getvalue()

    def _runner_init(self):
        original, debug = app.ApplicationRunner.__init__, self.debug

        def init(runner, browser, *a, **kw):
            kw.setdefault("answers", PROFILE)
            kw.setdefault("agent_output_dir", self.agent_out)
            original(runner, browser, *a, **kw)
            runner.debug_dir, runner.confirmation_wait = debug, 0
        return init

    def test_application_status(self):
        self.apply_until_unconfirmed()
        code, out = self.run_cli(main.run_application_status, self.job["id"], None)
        for expected in ("State: RECOVERY_REQUIRED", f"Code: {SUBMISSION_UNCONFIRMED}", "Apply clicked: yes",
                         "Application history: unconfirmed", "Score: 80/100", "Resume: v1.md",
                         "Idempotency: BLOCKED", "recover-application --job-id"):
            self.assertIn(expected, out)
        self.assertNotIn(SECRET_CTC, out)
        code, out = self.run_cli(main.run_application_status, None, 10)
        self.assertIn("RECOVERY_REQUIRED (1)", out)
        self.assertIn(f"[{self.job['id']}]", out)

    def test_recover_application_cli(self):
        self.apply_until_unconfirmed()
        code, out = self.run_cli(main.run_recover_application, self.job["id"], browser=FakeBrowser(pages=[snap(buttons=APPLIED)]))
        self.assertEqual(code, 0)
        self.assertIn("Result: ALREADY_APPLIED", out)
        self.assertEqual(self.fresh()["application_state"], "already_applied")


class TestAttemptMigration(unittest.TestCase):
    def test_existing_database_gains_attempt_table(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "jobs.db"
            with mock.patch.object(db, "DB_PATH", path):
                db.init_db()
                db.init_db()
                with sqlite3.connect(path) as conn:
                    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn("application_attempts", tables)
                self.assertIn("recovery_required", db.APPLICATION_STATES)


if __name__ == "__main__":
    unittest.main()


class TestSnapshotInBrowser(unittest.TestCase):
    """The real SNAPSHOT_JS on mock Naukri markup (live finding: Applied is a <span id="already-applied">)."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as e:
            raise unittest.SkipTest(f"Playwright Chromium unavailable: {e}")

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def snapshot(self, header_html, nav_html=""):
        page = self._browser.new_page()
        self.addCleanup(page.close)
        page.set_content(f'<nav>{nav_html}</nav><section id="job_header"><h1 class="styles_jd-header-title__x">Java Dev</h1>'
                         f'<div class="styles_jhc__apply-button-container__5Bqn">{header_html}</div></section>')
        return page.evaluate(app.SNAPSHOT_JS)

    def test_applied_span_in_header_confirms(self):
        snap_ = self.snapshot('<span id="already-applied" class="styles_already-applied__4KDhw already-applied">Applied</span>')
        self.assertTrue(detect_application_confirmation(snap_).confirmed)
        self.assertEqual(app.assess_job_page(dict(snap_, logged_in_markers=1, url=JOB_URL), JOB_URL).state, "already_applied")

    def test_applied_nav_link_alone_does_not_confirm(self):
        snap_ = self.snapshot('<button id="apply-button">Apply</button>', '<a href="/applied">Applied</a>')
        self.assertFalse(detect_application_confirmation(snap_).confirmed)
        self.assertTrue(any(b["id"] == "apply-button" and b["in_job_header"] for b in snap_["buttons"]))
