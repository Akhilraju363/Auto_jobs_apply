"""Phase 3: candidate selection, page assessment, approval gate, resume and question handling.

No real browser: a fake browser returns page snapshots and records every click.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import naukri_application as app
from naukri_parser import parse_card
from tests.pdf_fixture import RESUME_MD, write_pdf_for

JOB_URL = "https://www.naukri.com/job-listings-java-full-stack-developer-infosys-hyderabad-2-to-4-years-900000000001"
EASY = [{"id": "apply-button", "text": "Apply", "href": ""}]
APPLIED = [{"id": "already-applied", "text": "Applied", "href": ""}]
EXTERNAL = [{"id": "company-site-button", "text": "Apply on company site", "href": "https://careers.acme.com/42"}]
ANSWERS = {"total_experience": "4", "notice_period": "30 days", "expected_ctc": "12 LPA", "current_ctc": "",
           "current_location": "Hyderabad", "preferred_location": "", "phone": "", "email": ""}


def snap(buttons=EASY, auth="yes", body="Job description ...", url=JOB_URL, header=True, captcha=False, title="Java"):
    return {"url": url, "title": title, "body": body, "has_captcha_frame": captcha,
            "login_links": 1 if auth == "no" else 0, "logged_in_markers": 1 if auth == "yes" else 0,
            "has_job_header": header, "buttons": buttons}


class FakeBrowser:
    def __init__(self, pages=None, outcomes=None, chat=None):
        self.pages = list(pages or [snap()])
        self.outcomes = list(outcomes or [{"kind": "applied"}])
        self.chat = list(chat or [app.ChatState("applied", signature="applied")])  # scripted chat states
        self.opened, self.clicks, self.filled, self.submits = [], 0, [], 0
        self.answered = []  # (question text, value) entered into the chat
        self.last = None

    def chat_state(self):
        return self.chat.pop(0) if len(self.chat) > 1 else self.chat[0]

    def answer_chat(self, question, value):
        self.answered.append((question.text, value))

    def wait_for_chat_change(self, signature):
        return self.chat_state()

    confirmation = app.ConfirmationResult(False, None, "no Naukri confirmation signal")

    def await_confirmation(self, seconds, job_url=None):
        self.awaited = getattr(self, "awaited", 0) + 1
        return self.confirmation

    def open_job(self, url):
        self.opened.append(url)
        self.last = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        return self.last

    def snapshot(self):
        return self.last

    def click_easy_apply(self):
        self.clicks += 1

    def wait_for_outcome(self):
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]

    def fill_form(self, planned, resume):
        self.filled.append(planned)

    def submit_form(self):
        self.submits += 1


class Phase3TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite handles linger on Windows
        self.tmp = Path(self._tmp.name)
        patcher = mock.patch.object(db, "DB_PATH", self.tmp / "jobs.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        db.init_db()
        self.agent_out = self.tmp / "agent_output"
        self.make_resume()

    def add_job(self, job_id="900000000001", score=80.0, match_status="recommended", scored=True, url=None, **extra):
        card = {"job_id": job_id, "url": url or JOB_URL.replace("900000000001", job_id),
                "title": "Java Full Stack Developer", "company": "Infosys", "location": "Hyderabad"}
        job = parse_card(card).to_dict()
        job["job_description"] = "Spring Boot and Angular " * 10
        job.update(extra)
        db.save_discovered_job(job)
        row = next(j for j in db.get_jobs(status=None) if j["job_id"] == job_id)
        if scored:
            db.save_job_score(row["id"], score, match_status, "fits", ["Java"], ["Kafka"])
        return db.get_job(row["id"])

    def make_resume(self, url=JOB_URL, ext=".md", ok=True, saved=True, rid="abc123"):
        folder = self.agent_out / "generated_resumes" / rid
        folder.mkdir(parents=True, exist_ok=True)
        meta = {"id": rid, "updated_at": "2026-10-07T04:35:16+00:00",
                "job": {"job_key": url, "link": url + "?src=x", "source": "Naukri"},
                "versions": [{"n": 1, "validation": {"ok": ok}}]}
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (folder / f"v1{ext}").write_text(RESUME_MD, encoding="utf-8")
        records = [{"link": url, "status": "saved", "resume_link": "https://drive.google.com/file/d/X/view"}] if saved else []
        (self.agent_out / "tailored_jobs.json").write_text(json.dumps(records), encoding="utf-8")

    def runner(self, browser, answers=("y",), dry_run=False, profile=None):
        replies = list(answers)
        prompt = lambda msg: replies.pop(0) if replies else ""  # noqa: E731 -- Enter when the script runs out
        return app.ApplicationRunner(browser, prompt=prompt, out=lambda *_: None, dry_run=dry_run,
                                     answers=ANSWERS if profile is None else profile, agent_output_dir=self.agent_out)

    def applied_rows(self):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute("SELECT id, status FROM applied_jobs").fetchall()


class TestCandidates(Phase3TestCase):
    def test_selection_rules(self):
        rec = self.add_job("900000000001", 80, "recommended")
        self.add_job("900000000002", 70, "review")
        self.add_job("900000000003", 30, "skipped")
        self.add_job("900000000004", scored=False)
        self.add_job("900000000005", 90, "recommended", is_already_applied=True)
        done = self.add_job("900000000006", 90, "recommended")
        db.record_application_attempt(done, "applied")
        ext = self.add_job("900000000007", 85, "recommended")
        db.set_application_state(ext["id"], "external_application")
        self.assertEqual([j["id"] for j in db.get_application_candidates()], [rec["id"]])
        self.assertEqual(len(db.get_application_candidates(include_review=True)), 2)
        self.assertEqual(db.get_application_candidates(job_row_id=done["id"]), [])

    def test_retryable_states_stay_candidates(self):
        job = self.add_job()
        db.set_application_state(job["id"], "requires_login", "AUTH_REQUIRED")
        self.assertEqual(len(db.get_application_candidates()), 1)
        with self.assertRaises(ValueError):
            db.set_application_state(job["id"], "submitted_maybe")


class TestPageAssessment(unittest.TestCase):
    def test_auth_classification(self):
        self.assertEqual(app.classify_auth(snap(auth="yes")), "authenticated")
        self.assertEqual(app.classify_auth(snap(auth="no")), "login_required")
        self.assertEqual(app.classify_auth(snap(auth="unknown")), "unknown")
        self.assertEqual(app.classify_auth(snap(url="https://login.naukri.com/nLogin/Login.php")), "login_required")

    def test_states(self):
        cases = [
            (snap(), "ready", None),
            (snap(buttons=APPLIED), "already_applied", app.ALREADY_APPLIED),
            (snap(buttons=EXTERNAL), "external_application", app.EXTERNAL_APPLICATION),
            (snap(body="Sorry, this job has expired"), "job_unavailable", app.JOB_CLOSED),
            (snap(url="https://www.naukri.com/java-jobs", header=False), "job_unavailable", app.JOB_NOT_FOUND),
            (snap(auth="no"), "requires_login", app.AUTH_REQUIRED),
            (snap(auth="unknown"), "requires_login", app.AUTH_REQUIRED),
            (snap(captcha=True), "requires_manual_action", app.CAPTCHA_REQUIRED),
            (snap(title="Access Denied"), "requires_manual_action", app.CAPTCHA_REQUIRED),
            (snap(buttons=EASY + EXTERNAL), "requires_manual_action", app.UNKNOWN_UI_STATE),
            (snap(buttons=[]), "requires_manual_action", app.UNKNOWN_UI_STATE),
            (snap(buttons=[{"id": "apply-button", "text": "Apply", "disabled": True}]), "requires_manual_action",
             app.UNKNOWN_UI_STATE),
            (snap(buttons=[{"id": "x", "text": "Login to apply"}]), "requires_login", app.AUTH_REQUIRED),
        ]
        for snapshot, state, code in cases:
            result = app.assess_job_page(snapshot, JOB_URL)
            self.assertEqual((result.state, result.code), (state, code), snapshot)
        self.assertEqual(app.assess_job_page(snap(buttons=EXTERNAL), JOB_URL).external_url, "https://careers.acme.com/42")


class TestResume(Phase3TestCase):
    def test_finds_verified_tailored_resume_by_canonical_link(self):
        resume, why = app.find_tailored_resume(JOB_URL + "?utm=1", self.agent_out)
        self.assertEqual(why, "")
        self.assertEqual(resume.path.name, "v1.md")
        self.assertFalse(resume.uploadable)
        self.assertTrue(resume.drive_link.startswith("https://drive.google.com"))

    def test_prefers_exported_pdf(self):
        pdf = self.agent_out / "generated_resumes" / "abc123" / "v1.pdf"
        pdf.write_bytes(b"%PDF-1.4")  # a header alone is not a resume: rejected (Phase 3.2)
        resume, _ = app.find_tailored_resume(JOB_URL, self.agent_out)
        self.assertEqual((resume.path.suffix, resume.uploadable), (".md", False))
        write_pdf_for(pdf)
        resume, _ = app.find_tailored_resume(JOB_URL, self.agent_out)
        self.assertEqual((resume.path.suffix, resume.uploadable), (".pdf", True))

    def test_missing_and_unverified(self):
        self.assertIsNone(app.find_tailored_resume(JOB_URL.replace("0001", "0009"), self.agent_out)[0])
        self.assertIsNone(app.find_tailored_resume(JOB_URL, None)[0])
        self.make_resume(ok=False)
        self.assertIsNone(app.find_tailored_resume(JOB_URL, self.agent_out)[0])

    def test_drive_only_resume_is_not_found_locally(self):
        for meta in (self.agent_out / "generated_resumes").glob("*/meta.json"):
            meta.unlink()
        resume, why = app.find_tailored_resume(JOB_URL, self.agent_out)
        self.assertIsNone(resume)
        self.assertIn("only in Drive", why)

    def test_validate_resume_file(self):
        good = self.tmp / "r.pdf"
        good.write_bytes(b"%PDF")
        empty, wrong = self.tmp / "e.pdf", self.tmp / "r.exe"
        empty.write_bytes(b"")
        wrong.write_bytes(b"x")
        self.assertEqual(app.validate_resume_file(good), (True, ""))
        for path in (empty, wrong, self.tmp / "missing.pdf", self.tmp, None):
            self.assertFalse(app.validate_resume_file(path)[0], path)


class TestQuestions(unittest.TestCase):
    def test_classification(self):
        expected = {
            "Total experience (years)": "total_experience", "Notice period": "notice_period",
            "What is your expected package?": "expected_ctc", "Current CTC": "current_ctc",
            "Current location": "current_location", "Mobile number": "phone", "Email": "email",
            "Years of experience in Java": None, "Are you willing to relocate?": None,
            "Why should we hire you?": None, "Are you authorized to work in India?": None,
            "What is your age?": None, "Rate your Spring Boot skills": None, "Highest education": None,
        }
        self.assertEqual({q: app.classify_question(q) for q in expected}, expected)

    def test_plan_never_invents(self):
        questions = [
            {"index": 0, "type": "text", "label": "Total experience"},
            {"index": 1, "type": "select", "label": "Notice period", "options": ["15 days", "30 days", "60 days"]},
            {"index": 2, "type": "text", "label": "Current CTC"},  # not configured
            {"index": 3, "type": "textarea", "label": "Cover letter"},
            {"index": 4, "type": "select", "label": "Expected CTC", "options": ["5-8 LPA", "8-10 LPA"]},
        ]
        planned, unsupported = app.plan_answers(questions, ANSWERS)
        self.assertEqual([(q["index"], v) for q, v in planned], [(0, "4"), (1, "30 days")])
        self.assertEqual([q["index"] for q, _ in unsupported], [2, 3, 4])


class TestRunner(Phase3TestCase):
    def setUp(self):
        super().setUp()
        self.job = self.add_job()

    def state(self):
        return db.get_job(self.job["id"])["application_state"]

    def test_approved_submission_is_recorded_once(self):
        browser = FakeBrowser()
        result = self.runner(browser, ["y"]).process(self.job)
        self.assertEqual((result.state, result.submitted, browser.clicks), ("applied", True, 1))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])
        record = db.get_application_record(self.job)
        self.assertTrue(record["resume_path"].endswith("v1.md"))
        self.assertEqual(record["application_url"], JOB_URL)
        # second run: not a candidate any more, and a direct process() never clicks again
        self.assertEqual(db.get_application_candidates(), [])
        again = self.runner(FakeBrowser(), ["y"]).process(db.get_job(self.job["id"]))
        self.assertEqual(again.state, "already_applied")
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])
        self.assertFalse(db.record_application_attempt(self.job, "applied"))

    def test_no_approval_never_clicks(self):
        for answers in (["n"], ["s"], [""], ["maybe"], ["YES please"]):
            browser = FakeBrowser()
            result = self.runner(browser, answers).process(db.get_job(self.job["id"]))
            self.assertEqual((browser.clicks, result.submitted, result.state), (0, False, "ready"), answers)
        self.assertEqual(self.applied_rows(), [])

    def test_quit_and_eof_stop_without_clicking(self):
        browser = FakeBrowser()
        with self.assertRaises(app.ApplicationStop):
            self.runner(browser, ["q"]).process(self.job)

        def eof(_):
            raise EOFError

        runner = app.ApplicationRunner(browser, prompt=eof, out=lambda *_: None, agent_output_dir=self.agent_out)
        with self.assertRaises(app.ApplicationStop):
            runner.process(db.get_job(self.job["id"]))
        self.assertEqual(browser.clicks, 0)

    def test_dry_run_never_clicks_or_writes(self):
        browser = FakeBrowser()
        result = self.runner(browser, ["y"], dry_run=True).process(self.job)
        self.assertEqual((result.state, browser.clicks, result.submitted), ("ready", 0, False))
        self.assertIsNone(self.state())
        self.assertEqual(self.applied_rows(), [])

    def test_missing_resume_stops_before_browser(self):
        self.make_resume(url=JOB_URL.replace("0001", "0099"))
        browser = FakeBrowser()
        result = self.runner(browser).process(self.job)
        self.assertEqual((result.state, result.code, browser.opened, browser.clicks),
                         ("requires_manual_action", app.RESUME_NOT_FOUND, [], 0))

    def test_login_required_stops_run(self):
        browser = FakeBrowser(pages=[snap(auth="no")])
        with self.assertRaises(app.ApplicationStop) as stop:
            self.runner(browser).process(self.job)
        self.assertEqual((stop.exception.code, browser.clicks, self.state()), (app.AUTH_REQUIRED, 0, "requires_login"))

    def test_captcha_requires_manual_action(self):
        browser = FakeBrowser(pages=[snap(captcha=True)])
        with self.assertRaises(app.ApplicationStop):
            self.runner(browser, ["q"]).process(self.job)
        self.assertEqual((browser.clicks, self.state()), (0, "requires_manual_action"))
        cleared = FakeBrowser(pages=[snap(captcha=True)])
        cleared.snapshot = lambda: snap()  # the user solved it by hand, then pressed Enter
        result = self.runner(cleared, ["", "n"]).process(db.get_job(self.job["id"]))
        self.assertEqual((result.state, cleared.clicks), ("ready", 0))

    def test_non_ready_pages_are_recorded_without_clicking(self):
        for page, state in ((snap(buttons=APPLIED), "already_applied"), (snap(buttons=EXTERNAL), "external_application"),
                            (snap(body="This job has expired"), "job_unavailable"),
                            (snap(buttons=EASY + EXTERNAL), "requires_manual_action")):
            job = self.add_job(str(900000000100 + len(state)))
            self.make_resume(url=job["url"], rid=f"r{job['id']}")
            browser = FakeBrowser(pages=[page])
            result = self.runner(browser, ["y"]).process(job)
            self.assertEqual((result.state, browser.clicks), (state, 0))
            self.assertEqual(db.get_job(job["id"])["application_state"], state)
        self.assertEqual(self.applied_rows(), [])
        self.assertIsNotNone(db.get_job(job["id"]))

    def test_apply_control_failure_is_failed_not_recorded(self):
        browser = FakeBrowser()
        browser.click_easy_apply = mock.Mock(side_effect=RuntimeError("expected exactly one #apply-button"))
        result = self.runner(browser, ["y"]).process(self.job)
        self.assertEqual((result.state, result.code), ("failed", app.APPLICATION_FORM_CHANGED))
        self.assertEqual(self.applied_rows(), [])

    def test_unsupported_question_hands_over_and_never_submits_form(self):
        questions = [{"index": 0, "type": "text", "label": "Why do you want this job?"}]
        browser = FakeBrowser(outcomes=[{"kind": "form", "questions": questions}])
        result = self.runner(browser, ["y", ""]).process(self.job)
        # Phase 3.3: Apply was clicked, so an unfinished application needs recovery, not a blind re-run
        self.assertEqual((result.state, result.code, browser.submits, result.submitted),
                         ("recovery_required", app.UNSUPPORTED_QUESTION, 0, False))
        self.assertEqual(self.applied_rows(), [("900000000001", "incomplete")])

    def test_manual_completion_is_recorded_when_naukri_confirms(self):
        why = app.ChatState("question", app.ChatQuestion("Why do you want this job?", "text", input_ref="input-0",
                                                         save_ref="save-0"), signature="1|why", text="Why?")
        browser = FakeBrowser(outcomes=[{"kind": "chatbot"}],
                              chat=[why, app.ChatState("applied", signature="applied")])
        result = self.runner(browser, ["y", ""]).process(self.job)
        self.assertEqual((result.state, result.submitted), ("applied", True))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])

    def test_supported_questions_need_second_approval(self):
        questions = [{"index": 0, "type": "text", "label": "Total experience"},
                     {"index": 1, "type": "select", "label": "Notice period", "options": ["30 days", "60 days"]}]
        declined = FakeBrowser(outcomes=[{"kind": "form", "questions": questions}])
        self.runner(declined, ["y", "n"]).process(self.job)
        self.assertEqual((len(declined.filled), declined.submits), (1, 0))
        # a separate job: after a declined form the first job needs recovery (Phase 3.3)
        other = self.add_job("900000000002")
        self.make_resume(url=other["url"], rid="r2")
        approved = FakeBrowser(outcomes=[{"kind": "form", "questions": questions}, {"kind": "applied"}])
        result = self.runner(approved, ["y", "y"]).process(other)
        self.job = other
        self.assertEqual((approved.submits, result.state), (1, "applied"))
        self.assertEqual(db.get_application_record(self.job)["status"], "applied")

    def test_unconfirmed_attempt_is_upgraded_when_naukri_later_shows_applied(self):
        self.runner(FakeBrowser(outcomes=[{"kind": "unknown"}]), ["y"]).process(self.job)
        self.assertEqual(self.applied_rows(), [("900000000001", "unconfirmed")])
        # Phase 3.3: the upgrade happens through explicit recovery, never by re-running apply
        result = self.runner(FakeBrowser(pages=[snap(buttons=APPLIED)])).recover(db.get_job(self.job["id"]))
        self.assertEqual(result.state, "already_applied")
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])

    def test_external_redirect_after_apply(self):
        result = self.runner(FakeBrowser(outcomes=[{"kind": "new_tab", "url": "https://acme.wd1.myworkdayjobs.com/x"}]),
                             ["y"]).process(self.job)
        self.assertEqual((result.state, result.external_url), ("external_application", "https://acme.wd1.myworkdayjobs.com/x"))
        self.assertEqual(db.get_job(self.job["id"])["external_apply_url"], "https://acme.wd1.myworkdayjobs.com/x")

    def test_review_shows_scores_and_resume(self):
        resume, _ = app.find_tailored_resume(JOB_URL, self.agent_out)
        text = app.format_review(self.job, resume, app.Assessment("ready", reason="Naukri Apply button available"))
        for expected in ("APPLICATION REVIEW", "Match Score: 80/100", "- Java", "- Kafka", "v1.md",
                         "Approve this application?", JOB_URL):
            self.assertIn(expected, text)


def control(index, visible=True, enabled=True, text="Apply", in_job_header=True, in_sticky=False):
    return {"index": index, "visible": visible, "enabled": enabled, "text": text,
            "in_job_header": in_job_header, "in_sticky": in_sticky}


class FakeControl:
    """One #apply-button for the real NaukriApplicationBrowser, with a fake Playwright surface."""

    def __init__(self, **facts):
        self.facts = control(0, **facts)
        self.clicks = 0

    def is_visible(self):
        return self.facts["visible"]

    def is_enabled(self):
        return self.facts["enabled"]

    def inner_text(self):
        return self.facts["text"]

    def evaluate(self, script):
        if "isConnected" in script and "closest" not in script:
            return True
        return {"in_job_header": self.facts["in_job_header"], "in_sticky": self.facts["in_sticky"]}

    def click(self, timeout=None):
        self.clicks += 1


class FakeControls:
    def __init__(self, controls):
        self.controls = controls

    def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]

    @property
    def first(self):  # the resolver must never rely on this
        raise AssertionError(".first used on #apply-button")


class FakePage:
    def __init__(self, controls):
        self.controls = controls

    def locator(self, selector):
        assert selector == "#apply-button", selector
        return FakeControls(self.controls)


class TestApplyControlResolver(unittest.TestCase):
    def test_choose_cases(self):
        cases = {
            "one candidate": ([control(0)], 0),
            "hidden duplicate": ([control(0), control(1, visible=False, in_job_header=False, in_sticky=True)], 0),
            "hidden first": ([control(0, visible=False), control(1)], 1),
            "disabled duplicate": ([control(0, enabled=False), control(1)], 1),
            # live Naukri after scrolling: both visible, the sticky copy outside #job_header
            "scrolled sticky copy": ([control(0), control(1, in_job_header=False, in_sticky=True)], 0),
            "two actionable, no context": ([control(0, in_job_header=False), control(1, in_job_header=False)], None),
            "two in job header": ([control(0), control(1)], None),
            "no candidates": ([], None),
            "wrong text": ([control(0, text="Applied")], None),
        }
        for name, (candidates, expected) in cases.items():
            index, reason = app.choose_apply_control(candidates)
            self.assertEqual(index, expected, name)
            self.assertEqual(bool(reason), expected is None, name)

    def browser_with(self, controls):
        browser = app.NaukriApplicationBrowser(timeout_seconds=5)
        browser.page = FakePage(controls)
        return browser

    def test_clicks_only_the_resolved_control(self):
        header, sticky = FakeControl(), FakeControl(in_job_header=False, in_sticky=True)
        self.browser_with([header, sticky]).click_easy_apply()
        self.assertEqual((header.clicks, sticky.clicks), (1, 0))
        hidden, visible = FakeControl(visible=False), FakeControl()
        self.browser_with([hidden, visible]).click_easy_apply()
        self.assertEqual((hidden.clicks, visible.clicks), (0, 1))

    def test_ambiguous_or_missing_controls_click_nothing(self):
        for controls in ([FakeControl(in_job_header=False), FakeControl(in_job_header=False)],
                         [FakeControl(), FakeControl()], [], [FakeControl(enabled=False)]):
            with self.assertRaises(RuntimeError):
                self.browser_with(controls).click_easy_apply()
            self.assertTrue(all(c.clicks == 0 for c in controls))


class TestApplyControlInRunner(Phase3TestCase):
    """Cases 4-7: resolution failures are not applications; rejection and dry run never resolve."""

    class SpyBrowser(FakeBrowser):
        def __init__(self, controls, **kwargs):
            super().__init__(**kwargs)
            self.real = app.NaukriApplicationBrowser(timeout_seconds=5)
            self.real.page = FakePage(controls)
            self.resolves = 0

        def click_easy_apply(self):
            self.resolves += 1
            self.real.click_easy_apply()
            self.clicks += 1

    def setUp(self):
        super().setUp()
        self.job = self.add_job()

    def test_ambiguous_controls_fail_without_record(self):
        controls = [FakeControl(), FakeControl()]
        result = self.runner(self.SpyBrowser(controls), ["y"]).process(self.job)
        self.assertEqual((result.state, result.code, result.submitted), ("failed", app.APPLICATION_FORM_CHANGED, False))
        self.assertTrue(all(c.clicks == 0 for c in controls))
        self.assertEqual(self.applied_rows(), [])

    def test_rejection_and_dry_run_never_resolve(self):
        for answers, dry_run in ((["n"], False), (["s"], False), ([""], False), (["y"], True)):
            controls = [FakeControl()]
            browser = self.SpyBrowser(controls)
            self.runner(browser, answers, dry_run=dry_run).process(db.get_job(self.job["id"]))
            self.assertEqual((browser.resolves, controls[0].clicks), (0, 0), (answers, dry_run))

    def test_resolved_click_then_confirmation_records_once(self):
        header, sticky = FakeControl(), FakeControl(in_job_header=False, in_sticky=True)
        result = self.runner(self.SpyBrowser([header, sticky]), ["y"]).process(self.job)
        self.assertEqual((result.state, result.submitted, header.clicks, sticky.clicks), ("applied", True, 1, 0))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])


class TestApplicationHistoryMigration(unittest.TestCase):
    def test_existing_applied_jobs_rows_survive(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "jobs.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE applied_jobs (id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, "
                             "platform TEXT NOT NULL, url TEXT NOT NULL, match_score REAL NOT NULL, "
                             "status TEXT NOT NULL DEFAULT 'pending', applied_at TIMESTAMP NOT NULL)")
                conn.execute("INSERT INTO applied_jobs VALUES ('old', 't', 'c', 'Naukri', 'u', 50, 'applied', '2026-01-01')")
            with mock.patch.object(db, "DB_PATH", path):
                db.init_db()
                db.init_db()
                with sqlite3.connect(path) as conn:
                    columns = {r[1] for r in conn.execute("PRAGMA table_info(applied_jobs)")}
                    rows = conn.execute("SELECT id, status, resume_path FROM applied_jobs").fetchall()
                self.assertTrue(set(db.APPLIED_JOBS_EXTRA_COLUMNS) <= columns)
                self.assertEqual(rows, [("old", "applied", None)])
                self.assertEqual(db.get_applied_jobs_count(), 1)


if __name__ == "__main__":
    unittest.main()
