"""Phase 3.2: verified resume artifact resolution, validation and Naukri upload safety."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import db
import naukri_application as app
import resume_artifact as ra
from tests.pdf_fixture import RESUME_MD, make_pdf, write_docx_for, write_pdf_for
from tests.test_naukri_application import JOB_URL, FakeBrowser, Phase3TestCase

RID = "810b8ec2012d"
OTHER_URL = "https://www.naukri.com/job-listings-other-role-other-co-pune-1-to-3-years-111111111111"


def sha(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class AgentOutput:
    """A temp AI Agent output/ folder with one resume record."""

    def __init__(self, root: Path):
        self.out = root / "agent_output"
        self.folder = self.out / "generated_resumes" / RID
        self.folder.mkdir(parents=True)

    def record(self, job_key=JOB_URL, versions=((RESUME_MD, True),), artifacts=None, saved=True, saved_extra=None):
        meta = {"id": RID, "updated_at": "2026-10-07T05:00:00+00:00", "master_version": "abc",
                "job": {"job_key": job_key, "link": job_key, "source": "Naukri"}, "versions": []}
        for n, (markdown, ok) in enumerate(versions, 1):
            (self.folder / f"v{n}.md").write_text(markdown, encoding="utf-8")
            meta["versions"].append({"n": n, "validation": {"ok": ok}, "exports": {},
                                     "artifacts": (artifacts or {}).get(n, {})})
        (self.folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        records = []
        if saved:
            records.append({"link": JOB_URL, "status": "saved", "resume_link": "https://drive.google.com/file/d/X/view",
                            "resume_id": RID, **(saved_extra or {})})
        (self.out / "tailored_jobs.json").write_text(json.dumps(records), encoding="utf-8")
        return self

    def published(self, n=1, markdown=RESUME_MD, job_key=JOB_URL, verified=True):
        return {n: {"pdf": {"file": f"v{n}.pdf", "version": n, "verified": verified, "md_sha1": sha(markdown),
                            "job_key": job_key}}}


class TestResolver(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.agent = AgentOutput(Path(self._tmp.name))

    def resolve(self, url=JOB_URL):
        return ra.resolve_verified_resume(url, self.agent.out)

    def test_verified_markdown_and_pdf(self):
        self.agent.record(artifacts=self.agent.published())
        write_pdf_for(self.agent.folder / "v1.pdf")
        artifact, code, _ = self.resolve(JOB_URL + "?src=jobsearch")
        self.assertIsNone(code)
        self.assertEqual((artifact.version, artifact.verified, artifact.uploadable), (1, True, True))
        self.assertEqual(artifact.upload_path.name, "v1.pdf")
        self.assertEqual(artifact.markdown_path.name, "v1.md")
        self.assertTrue(any("published for this job" in c for c in artifact.checks))

    def test_markdown_only_is_reviewable_not_uploadable(self):
        self.agent.record()
        artifact, code, _ = self.resolve()
        self.assertIsNone(code)
        self.assertEqual((artifact.uploadable, artifact.upload_code), (False, ra.RESUME_PDF_NOT_FOUND))

    def test_unverified_resume(self):
        self.agent.record(versions=((RESUME_MD, False),))
        write_pdf_for(self.agent.folder / "v1.pdf")
        self.assertEqual(self.resolve()[:2], (None, ra.RESUME_NOT_VERIFIED))

    def test_unverified_pdf_artifact(self):
        self.agent.record(artifacts=self.agent.published(verified=False))
        write_pdf_for(self.agent.folder / "v1.pdf")
        artifact, _, _ = self.resolve()
        self.assertEqual((artifact.uploadable, artifact.upload_code), (False, ra.RESUME_NOT_VERIFIED))

    def test_record_for_another_job(self):
        self.agent.record(job_key=OTHER_URL)
        write_pdf_for(self.agent.folder / "v1.pdf")
        self.assertEqual(self.resolve()[:2], (None, ra.RESUME_ARTIFACT_MISMATCH))

    def test_pdf_published_for_another_job(self):
        self.agent.record(artifacts=self.agent.published(job_key=OTHER_URL))
        write_pdf_for(self.agent.folder / "v1.pdf")
        artifact, _, _ = self.resolve()
        self.assertEqual((artifact.uploadable, artifact.upload_code), (False, ra.RESUME_ARTIFACT_MISMATCH))

    def test_corrupt_pdfs(self):
        self.agent.record()
        for data in (b"%PDF-1.4 garbage" * 50, make_pdf(["x"] * 40)[:400], b"<html>Google Drive</html>" * 50):
            (self.agent.folder / "v1.pdf").write_bytes(data)
            artifact, _, _ = self.resolve()
            self.assertEqual((artifact.uploadable, artifact.upload_code), (False, ra.RESUME_PDF_INVALID), data[:20])

    def test_pdf_without_meaningful_text(self):
        self.agent.record()
        (self.agent.folder / "v1.pdf").write_bytes(make_pdf([]))
        artifact, _, reason = self.resolve()
        self.assertEqual(artifact.upload_code, ra.RESUME_PDF_VALIDATION_FAILED)
        self.assertIn("no meaningful text", reason or artifact.upload_reason)

    def test_pdf_of_a_different_resume(self):
        self.agent.record()  # a readable PDF, but of someone else's resume
        write_pdf_for(self.agent.folder / "v1.pdf", "# Someone Else\n## Profile\n" + "Ruby developer " * 40)
        artifact, _, _ = self.resolve()
        self.assertEqual(artifact.upload_code, ra.RESUME_PDF_VALIDATION_FAILED)

    def test_stale_pdf_older_than_markdown(self):
        self.agent.record(artifacts=self.agent.published(markdown=RESUME_MD + "- old line\n"))
        write_pdf_for(self.agent.folder / "v1.pdf")
        artifact, _, _ = self.resolve()
        self.assertEqual(artifact.upload_code, ra.RESUME_PDF_VALIDATION_FAILED)
        self.assertIn("older", artifact.upload_reason)

    def test_docx_fallback(self):
        self.agent.record()
        write_docx_for(self.agent.folder / "v1.docx")
        artifact, _, _ = self.resolve()
        self.assertEqual((artifact.uploadable, artifact.upload_path.suffix), (True, ".docx"))

    def test_saved_version_is_used_not_a_newer_or_older_one(self):
        v2 = RESUME_MD + "- Led migration of legacy services to Kubernetes clusters\n"
        self.agent.record(versions=((RESUME_MD, True), (v2, True)), saved_extra={"resume_version": 1})
        write_pdf_for(self.agent.folder / "v2.pdf", v2)  # only v2 has a PDF
        artifact, _, _ = self.resolve()
        self.assertEqual((artifact.version, artifact.uploadable, artifact.upload_code), (1, False, ra.RESUME_PDF_NOT_FOUND))

    def test_master_resume_is_never_substituted(self):
        master = self.agent.out.parent / "resume"
        master.mkdir()
        (master / "base_resume.md").write_text(RESUME_MD, encoding="utf-8")
        write_pdf_for(master / "base_resume.pdf")
        self.assertEqual(self.resolve()[:2], (None, ra.RESUME_NOT_FOUND))

    def test_missing_output_dir(self):
        self.assertEqual(ra.resolve_verified_resume(JOB_URL, None)[1], ra.RESUME_NOT_FOUND)


class TestUploadControlResolver(unittest.TestCase):
    def control(self, ref, disabled=False, visible=True, accept="", resume=True):
        return {"ref": ref, "disabled": disabled, "in_visible_area": visible, "accept": accept, "resume_context": resume}

    def test_cases(self):
        c = self.control
        cases = {
            "single": ([c("0")], ".pdf", "0"),
            "hidden duplicate": ([c("0", visible=False), c("1")], ".pdf", "1"),
            "disabled duplicate": ([c("0", disabled=True), c("1")], ".pdf", "1"),
            "two usable, one labelled resume": ([c("0", resume=False), c("1")], ".pdf", "1"),
            "two usable, both resume": ([c("0"), c("1")], ".pdf", None),
            "two usable, no labels": ([c("0", resume=False), c("1", resume=False)], ".pdf", None),
            "accepts pdf": ([c("0", accept=".pdf,.doc,.docx")], ".pdf", "0"),
            "accepts mime": ([c("0", accept="application/pdf")], ".pdf", "0"),
            "wrong type": ([c("0", accept=".doc,.docx")], ".pdf", None),
            "none": ([], ".pdf", None),
        }
        for name, (inputs, suffix, expected) in cases.items():
            self.assertEqual(app.choose_upload_control(inputs, suffix)[0], expected, name)

    def test_recruiter_facing_file_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = Path(tmp) / "v1.md"
            md.write_text(RESUME_MD, encoding="utf-8")
            self.assertEqual(app.upload_file_name(md, ".pdf"), "Test_Candidate_Resume.pdf")
            self.assertEqual(app.upload_file_name(Path(tmp) / "missing.md", ".pdf"), "Resume.pdf")


UPLOAD_PAGE = """
<html><body>
<div role="dialog" class="apply-modal">
  <p>Upload Resume</p>
  __CONTROLS__
  <div id="status"></div>
</div>
<script>
  document.querySelectorAll("input[type=file]").forEach(input => input.addEventListener('change', () => {
    if (__CONFIRM__) document.getElementById('status').textContent = input.files[0].name;
  }));
</script>
</body></html>
"""
CONTROL = '<label class="upload">Upload resume <input type="file" accept=".pdf,.doc,.docx" style="display:none"></label>'
HIDDEN_CONTROL = '<div style="display:none"><label>Resume <input type="file"></label></div>'


class TestUploadInBrowser(unittest.TestCase):
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

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        (tmp / "v1.md").write_text(RESUME_MD, encoding="utf-8")
        self.resume = app.ResumeInfo(write_pdf_for(tmp / "v1.pdf"), True, resume_id=RID, version=1,
                                     markdown_path=tmp / "v1.md")

    def browser_on(self, controls, confirm=True):
        page = self._browser.new_page()
        self.addCleanup(page.close)
        page.set_content(UPLOAD_PAGE.replace("__CONTROLS__", controls).replace("__CONFIRM__", "true" if confirm else "false"))
        browser = app.NaukriApplicationBrowser(timeout_seconds=2)
        browser.page = page
        return browser

    def test_uploads_named_file_and_confirms(self):
        browser = self.browser_on(CONTROL)
        browser.upload_resume(self.resume)
        self.assertEqual(browser.page.inner_text("#status"), "Test_Candidate_Resume.pdf")

    def test_hidden_duplicate_is_ignored(self):
        browser = self.browser_on(HIDDEN_CONTROL + CONTROL)
        browser.upload_resume(self.resume)
        self.assertEqual(browser.page.inner_text("#status"), "Test_Candidate_Resume.pdf")

    def test_duplicate_or_missing_controls_fail_closed(self):
        for controls in (CONTROL + CONTROL, ""):
            browser = self.browser_on(controls)
            with self.assertRaises(app.UploadError) as err:
                browser.upload_resume(self.resume)
            self.assertEqual(err.exception.code, app.RESUME_UPLOAD_FORM_CHANGED)
            self.assertEqual(browser.page.inner_text("#status"), "")

    def test_unconfirmed_upload(self):
        browser = self.browser_on(CONTROL, confirm=False)
        with self.assertRaises(app.UploadError) as err:
            browser.upload_resume(self.resume)
        self.assertEqual(err.exception.code, app.RESUME_UPLOAD_NOT_CONFIRMED)

    def test_non_uploadable_resume_is_refused_before_touching_the_page(self):
        browser = self.browser_on(CONTROL)
        md_only = app.ResumeInfo(self.resume.markdown_path, False, upload_code=ra.RESUME_PDF_NOT_FOUND)
        with self.assertRaises(app.UploadError) as err:
            browser.upload_resume(md_only)
        self.assertEqual(err.exception.code, ra.RESUME_PDF_NOT_FOUND)
        self.assertEqual(browser.page.inner_text("#status"), "")


class UploadSpyBrowser(FakeBrowser):
    def __init__(self, fail=None, **kwargs):
        super().__init__(**kwargs)
        self.uploads, self.saves, self.fail = [], 0, fail

    def upload_resume(self, resume):
        if self.fail:
            raise app.UploadError(self.fail, "simulated")
        self.uploads.append(resume.path.name)

    def click_chat_save(self, question):
        self.saves += 1


class TestUploadInApplicationFlow(Phase3TestCase):
    def setUp(self):
        super().setUp()
        self.job = self.add_job()
        folder = self.agent_out / "generated_resumes" / "abc123"
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        meta["versions"][0]["artifacts"] = {"pdf": {"file": "v1.pdf", "version": 1, "verified": True,
                                                    "md_sha1": sha(RESUME_MD), "job_key": JOB_URL}}
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        write_pdf_for(folder / "v1.pdf")
        file_q = app.parse_chat({"present": True, "roots": 1, "question": "Please upload your resume",
                                 "message_count": 2, "inputs": [], "options": [], "checkbox_count": 0,
                                 "saves": [{"ref": "save-0", "text": "Save"}], "skip_count": 0, "file_count": 1})
        self.assertEqual(file_q.question.kind, "file")
        self.file_q = file_q
        self.applied = app.ChatState("applied", signature="applied")

    def run_job(self, browser, prompts=("y",), dry_run=False):
        return self.runner(browser, list(prompts), dry_run=dry_run).process(db.get_job(self.job["id"]))

    def test_upload_requested_in_chat(self):
        browser = UploadSpyBrowser(outcomes=[{"kind": "chatbot"}], chat=[self.file_q, self.applied])
        result = self.run_job(browser)
        self.assertEqual((browser.uploads, browser.saves, result.state), (["v1.pdf"], 1, "applied"))
        self.assertTrue(db.get_application_record(self.job)["resume_path"].endswith("v1.pdf"))

    def test_no_validated_pdf_hands_over(self):
        (self.agent_out / "generated_resumes" / "abc123" / "v1.pdf").unlink()
        browser = UploadSpyBrowser(outcomes=[{"kind": "chatbot"}], chat=[self.file_q])
        result = self.run_job(browser, prompts=("y", "s"))
        self.assertEqual((browser.uploads, result.code), ([], ra.RESUME_PDF_NOT_FOUND))

    def test_upload_failure_hands_over(self):
        browser = UploadSpyBrowser(fail=app.RESUME_UPLOAD_FORM_CHANGED, outcomes=[{"kind": "chatbot"}], chat=[self.file_q])
        result = self.run_job(browser, prompts=("y", "s"))
        self.assertEqual((result.state, result.code), ("recovery_required", app.RESUME_UPLOAD_FORM_CHANGED))

    def test_dry_run_and_rejection_never_upload(self):
        for prompts, dry in ((["y"], True), (["n"], False), ([""], False)):
            browser = UploadSpyBrowser(outcomes=[{"kind": "chatbot"}], chat=[self.file_q, self.applied])
            self.run_job(browser, prompts, dry_run=dry)
            self.assertEqual((browser.clicks, browser.uploads), (0, []), (prompts, dry))

    def test_dry_run_reports_the_upload_candidate(self):
        outputs = []
        runner = app.ApplicationRunner(FakeBrowser(), prompt=lambda _: "y", out=outputs.append, dry_run=True,
                                       answers={}, agent_output_dir=self.agent_out)
        runner.process(self.job)
        text = "\n".join(outputs)
        for expected in ("Verified resume: YES", "PDF available: YES", "Upload candidate:", "v1.pdf",
                         "Nothing was clicked, uploaded, answered, submitted or recorded"):
            self.assertIn(expected, text)

    def test_wrong_job_artifact_stops_before_browser(self):
        folder = self.agent_out / "generated_resumes" / "abc123"
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        meta["versions"][0]["artifacts"]["pdf"]["job_key"] = OTHER_URL
        (folder / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        browser = UploadSpyBrowser()
        result = self.run_job(browser)
        self.assertEqual((result.code, browser.opened, browser.uploads), (ra.RESUME_ARTIFACT_MISMATCH, [], []))


if __name__ == "__main__":
    unittest.main()
