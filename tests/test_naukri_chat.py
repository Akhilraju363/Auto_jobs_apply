"""Phase 3.1: Naukri chat-style recruiter questions.

- parse_chat(): fail-closed structure rules (pure)
- ApplicationRunner question loop with a scripted fake browser
- the real CHAT_DESCRIBE_JS / answer_chat against a local mock of Naukri's chat panel
  (headless Chromium on set_content, no network; skipped when no Playwright browser is installed)
"""

import logging
import unittest

import db
import naukri_application as app
from application_profile import ApplicationProfile, ResumeEvidence
from tests.test_naukri_application import FakeBrowser, Phase3TestCase

SECRET_CTC = "17.25"  # a value that must never show up in logs
PROFILE = ApplicationProfile.from_mapping({"expected_ctc": SECRET_CTC, "notice_period": "30 days",
                                           "willing_to_relocate": "true"})


def desc(question="What is your expected CTC in Lacs per annum?", inputs=1, options=(), saves=1, **extra):
    data = {
        "present": True, "roots": 1, "question": question, "message_count": 2,
        "inputs": [{"ref": f"input-{i}", "kind": "contenteditable"} for i in range(inputs)],
        "options": [{"ref": f"option-{i}", "text": t} for i, t in enumerate(options)],
        "checkbox_count": 0, "saves": [{"ref": f"save-{i}", "text": "Save"} for i in range(saves)], "skip_count": 0,
    }
    data.update(extra)
    return data


def question_state(text, kind="text", options=(), count=2):
    parsed = app.parse_chat(desc(text, inputs=0 if options else 1, options=options, message_count=count))
    assert parsed.kind == "question", parsed
    return parsed


class TestParseChat(unittest.TestCase):
    def test_text_question(self):
        state = app.parse_chat(desc())
        self.assertEqual((state.kind, state.question.kind, state.question.input_ref, state.question.save_ref),
                         ("question", "text", "input-0", "save-0"))
        self.assertEqual(state.signature, "2|What is your expected CTC in Lacs per annum?")

    def test_radio_question(self):
        state = app.parse_chat(desc("Are you willing to relocate?", inputs=0, options=("Yes", "No")))
        self.assertEqual((state.question.kind, state.question.options), ("radio", ["Yes", "No"]))
        self.assertEqual(state.question.option_refs["No"], "option-1")

    def test_fail_closed_structures(self):
        cases = {
            "gone": ({"present": False}, "gone", None),
            "two panels": ({"present": True, "roots": 2}, "error", app.QUESTION_FORM_CHANGED),
            "no question": (desc(question=""), "error", app.QUESTION_FORM_CHANGED),
            "duplicate inputs": (desc(inputs=2), "error", app.QUESTION_FORM_CHANGED),
            "missing input": (desc(inputs=0), "error", app.QUESTION_INPUT_NOT_FOUND),
            "duplicate save": (desc(saves=2), "error", app.QUESTION_FORM_CHANGED),
            "missing save": (desc(saves=0), "error", app.QUESTION_SAVE_NOT_FOUND),
            "input and choices": (desc(options=("Yes", "No")), "error", app.QUESTION_FORM_CHANGED),
            "checkboxes": (desc(checkbox_count=3), "error", app.QUESTION_FORM_CHANGED),
            "duplicate choices": (desc(inputs=0, options=("Yes", "yes")), "error", app.QUESTION_FORM_CHANGED),
        }
        for name, (description, kind, code) in cases.items():
            state = app.parse_chat(description)
            self.assertEqual((state.kind, state.code), (kind, code), name)
            self.assertIsNone(state.question, name)


class TestChatRunner(Phase3TestCase):
    def setUp(self):
        super().setUp()
        self.job = self.add_job()

    def run_chat(self, chat, prompts=("y",), profile=PROFILE, evidence=None):
        browser = FakeBrowser(outcomes=[{"kind": "chatbot"}], chat=chat)
        runner = app.ApplicationRunner(browser, prompt=lambda _: prompts_left.pop(0) if prompts_left else "",
                                       out=lambda *_: None, answers=profile, agent_output_dir=self.agent_out,
                                       evidence=evidence or ResumeEvidence("Java, Spring Boot"))
        prompts_left = list(prompts)
        return runner.process(db.get_job(self.job["id"])), browser

    def test_known_questions_auto_answered_until_confirmation(self):
        ctc = question_state("What is your expected CTC in Lacs per annum?", count=2)
        notice = question_state("How soon can you join?", count=4)
        relocate = question_state("Are you willing to relocate?", "radio", ("Yes", "No"), count=6)
        applied = app.ChatState("applied", signature="applied")
        result, browser = self.run_chat([ctc, notice, notice, relocate, relocate, applied])
        self.assertEqual(browser.answered, [("What is your expected CTC in Lacs per annum?", SECRET_CTC),
                                            ("How soon can you join?", "30 days"),
                                            ("Are you willing to relocate?", "Yes")])
        self.assertEqual((result.state, result.submitted), ("applied", True))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])

    def test_unknown_question_goes_to_human_then_continues(self):
        ctc = question_state("Expected CTC?", count=2)
        why = question_state("Why do you want to join Infosys?", count=4)
        applied = app.ChatState("applied", signature="applied")
        # ctc auto -> why (manual: user answers in browser, presses Enter) -> applied
        result, browser = self.run_chat([ctc, why, why, applied, applied], prompts=("y", ""))
        self.assertEqual(browser.answered, [("Expected CTC?", SECRET_CTC)])
        self.assertEqual(result.state, "applied")

    def test_manual_question_not_resolved_until_it_changes(self):
        why = question_state("Tell us about yourself", count=2)
        applied = app.ChatState("applied", signature="applied")
        outputs = []
        # shown -> Enter: still open -> Enter: resolved
        browser = FakeBrowser(outcomes=[{"kind": "chatbot"}], chat=[why, why, applied])
        replies = ["y", "", ""]
        runner = app.ApplicationRunner(browser, prompt=lambda _: replies.pop(0) if replies else "s",
                                       out=outputs.append, answers=PROFILE, agent_output_dir=self.agent_out,
                                       evidence=ResumeEvidence())
        result = runner.process(self.job)
        self.assertEqual(result.state, "applied")
        self.assertTrue(any("still appears unanswered" in o for o in outputs))
        self.assertTrue(any("MANUAL APPLICATION QUESTION" in o and "Tell us about yourself" in o for o in outputs))
        self.assertEqual(browser.answered, [])

    def test_unconfigured_question_never_answered_and_stop_keeps_job_open(self):
        ctc = question_state("What is your expected CTC?", count=2)
        result, browser = self.run_chat([ctc], prompts=("y", "s"), profile=ApplicationProfile())
        self.assertEqual(browser.answered, [])
        self.assertEqual((result.state, result.code, result.submitted), ("recovery_required", "UNCONFIGURED_ANSWER", False))
        self.assertEqual(self.applied_rows(), [("900000000001", "incomplete")])
        self.assertNotIn("applied", [r[1] for r in self.applied_rows()])

    def test_answer_not_accepted_hands_over(self):
        ctc = question_state("Expected CTC?", count=2)
        result, browser = self.run_chat([ctc, ctc, ctc], prompts=("y", "s"))
        self.assertEqual(len(browser.answered), 1)  # entered once, never retried blindly
        self.assertEqual((result.state, result.code), ("recovery_required", app.QUESTION_FORM_CHANGED))

    def test_ui_errors_and_closed_chat(self):
        broken = app.parse_chat(desc(saves=2))
        result, browser = self.run_chat([broken], prompts=("y", "s"))
        self.assertEqual((result.code, browser.answered), (app.QUESTION_FORM_CHANGED, []))
        self.assertEqual(result.state, "recovery_required")  # Apply was clicked (Phase 3.3)
        # a closed chat without confirmation (fresh job: the first one now needs recovery)
        self.job = self.add_job("900000000002")
        self.make_resume(url=self.job["url"], rid="r2")
        gone = app.ChatState("gone", signature="gone")
        result, _ = self.run_chat([gone])
        self.assertEqual((result.state, result.code), ("recovery_required", app.SUBMISSION_UNCONFIRMED))
        self.assertEqual(self.applied_rows()[-1], ("900000000002", "unconfirmed"))

    def test_rejected_application_never_reaches_questions(self):
        result, browser = self.run_chat([question_state("Expected CTC?")], prompts=("n",))
        self.assertEqual((browser.clicks, browser.answered, result.state), (0, [], "ready"))

    def test_applied_record_is_not_overwritten_by_a_retry(self):
        self.run_chat([app.ChatState("applied", signature="applied")])
        result, browser = self.run_chat([question_state("Expected CTC?")])
        self.assertEqual((result.state, browser.clicks), ("already_applied", 0))
        self.assertEqual(self.applied_rows(), [("900000000001", "applied")])

    def test_values_never_logged(self):
        ctc = question_state("What is your expected CTC?", count=2)
        with self.assertLogs("naukri_application", level=logging.INFO) as logs:
            self.run_chat([ctc, app.ChatState("applied", signature="applied")])
        text = "\n".join(logs.output)
        self.assertIn("category=expected_ctc", text)
        self.assertIn("source=application_profile", text)
        self.assertIn("action=auto_answered", text)
        self.assertNotIn(SECRET_CTC, text)


MOCK_CHAT = """
<html><body>
<div id="root"><button id="apply-button">Applied</button></div>
<div class="chatbot_DrawerContentWrapper">
  <div class="chatbot_MessageContainer"><ul id="msgs">
    <li class="botItem chatbot_ListItem"><div class="botMsg msg"><div><span>Hi Akhil, thank you for showing interest.</span></div></div></li>
    <li class="botItem chatbot_ListItem"><div class="botMsg msg"><div><span id="q">What is your expected CTC in Lacs per annum?</span></div></div></li>
  </ul></div>
  __INPUTS__
  <div class="chatbot_Chip chipInRow"><span>Skip this question</span></div>
  __SAVES__
</div>
<script>
  const msgs = document.getElementById('msgs');
  window.answers = [];
  const next = ['How soon can you join?', null];
  function addMsg(cls, text) {
    const li = document.createElement('li'); li.className = cls;
    li.innerHTML = '<div class="' + (cls.startsWith('bot') ? 'botMsg' : 'userMsg') + '"><span></span></div>';
    li.querySelector('span').textContent = text; msgs.appendChild(li);
  }
  document.querySelectorAll('.sendMsg').forEach(btn => btn.addEventListener('click', () => {
    const box = document.querySelector("[contenteditable='true']");
    const picked = document.querySelector("input[type=radio]:checked");
    const value = box ? box.innerText.trim() : (picked ? picked.value : '');
    if (!value) return;
    window.answers.push(value); addMsg('userItem', value); if (box) box.innerText = '';
    const q = next.shift();
    if (q) addMsg('botItem chatbot_ListItem', q);
    else { document.querySelector('.chatbot_DrawerContentWrapper').remove();
           document.body.insertAdjacentHTML('beforeend', '<p>You have successfully applied to this job.</p>'); }
  }));
</script>
</body></html>
"""
TEXT_INPUT = '<div class="chatbot_InputContainer"><div class="textArea" contenteditable="true" id="userInput__xInputBox"></div></div>'
SAVE = '<div class="sendMsgbtn_container"><div class="send"><div class="sendMsg">Save</div></div></div>'


def mock_page(inputs=TEXT_INPUT, saves=SAVE):
    return MOCK_CHAT.replace("__INPUTS__", inputs).replace("__SAVES__", saves)


class TestChatUiInBrowser(unittest.TestCase):
    """Real CHAT_DESCRIBE_JS + Playwright actions against a local mock of Naukri's chat panel."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as e:  # no browser installed: the pure tests above still cover the logic
            raise unittest.SkipTest(f"Playwright Chromium unavailable: {e}")

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def browser_on(self, html):
        page = self._browser.new_page()
        self.addCleanup(page.close)
        page.set_content(html)
        browser = app.NaukriApplicationBrowser(timeout_seconds=5)
        browser.page = page
        return browser

    def test_detects_answers_saves_and_moves_to_next_question(self):
        browser = self.browser_on(mock_page())
        state = parse = app.parse_chat(browser.page.evaluate(app.CHAT_DESCRIBE_JS))
        self.assertEqual(parse.kind, "question")
        self.assertEqual(state.question.text, "What is your expected CTC in Lacs per annum?")
        self.assertTrue(state.question.skip_available)
        browser.answer_chat(state.question, "12")
        second = app.parse_chat(browser.page.evaluate(app.CHAT_DESCRIBE_JS))
        self.assertEqual(second.question.text, "How soon can you join?")
        self.assertNotEqual(second.signature, state.signature)
        browser.answer_chat(second.question, "30 days")
        self.assertEqual(browser.page.evaluate("window.answers"), ["12", "30 days"])
        self.assertFalse(browser.page.evaluate(app.CHAT_DESCRIBE_JS)["present"])
        self.assertIn("successfully applied", browser.page.inner_text("body").lower())

    def test_skip_is_never_clicked(self):
        browser = self.browser_on(mock_page())
        state = app.parse_chat(browser.page.evaluate(app.CHAT_DESCRIBE_JS))
        browser.answer_chat(state.question, "12")
        self.assertEqual(browser.page.evaluate("window.answers"), ["12"])  # Save, not Skip, was clicked

    def test_radio_question(self):
        radios = ('<div class="ssrc__radio-btn-container"><input type="radio" id="r1" name="rel" value="Yes">'
                  '<label for="r1">Yes</label><input type="radio" id="r2" name="rel" value="No"><label for="r2">No</label></div>')
        browser = self.browser_on(mock_page(inputs=radios))
        state = app.parse_chat(browser.page.evaluate(app.CHAT_DESCRIBE_JS))
        self.assertEqual((state.question.kind, state.question.options), ("radio", ["Yes", "No"]))
        browser.answer_chat(state.question, "No")
        self.assertEqual(browser.page.evaluate("window.answers"), ["No"])

    def test_ambiguous_ui_fails_closed(self):
        cases = {
            "duplicate inputs": (mock_page(inputs=TEXT_INPUT + TEXT_INPUT), app.QUESTION_FORM_CHANGED),
            "missing input": (mock_page(inputs=""), app.QUESTION_INPUT_NOT_FOUND),
            "duplicate save": (mock_page(saves=SAVE + SAVE), app.QUESTION_FORM_CHANGED),
            "missing save": (mock_page(saves=""), app.QUESTION_SAVE_NOT_FOUND),
        }
        for name, (html, code) in cases.items():
            browser = self.browser_on(html)
            state = app.parse_chat(browser.page.evaluate(app.CHAT_DESCRIBE_JS))
            self.assertEqual((state.kind, state.code), ("error", code), name)
            self.assertEqual(browser.page.evaluate("window.answers"), [], name)

    def test_stale_marker_fails_instead_of_typing_elsewhere(self):
        browser = self.browser_on(mock_page())
        state = app.parse_chat(browser.page.evaluate(app.CHAT_DESCRIBE_JS))
        browser.page.evaluate("document.querySelector(\"[contenteditable='true']\").removeAttribute('data-aja')")
        with self.assertRaises(RuntimeError):
            browser.answer_chat(state.question, "12")
        self.assertEqual(browser.page.evaluate("window.answers"), [])


if __name__ == "__main__":
    unittest.main()
