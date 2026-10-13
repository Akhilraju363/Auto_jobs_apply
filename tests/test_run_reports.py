"""Run folders, run.log, result.json, --last-run, the workflow guard and the read-only --check-config.

Every test uses temporary folders and a temporary database (Phase3TestCase). Naukri, the AI Agent and
Google Sheets are fakes; the only real subprocesses are tiny local Python one-liners. Nothing here
can scan, score, upload or apply.
"""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from unittest import mock

import application_pipeline as pipeline
import db
from ai_agent_bridge import AgentRunResult
from tests.test_application_pipeline import FakeScanner, PipelineTestCase

PREP_ENV = {"artifact_sync": "0"}


def snapshot(root: Path) -> dict:
    """Every file under root -> (sha1, mtime_ns); folders too, so a new folder is noticed."""
    state = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_dir():
            state[str(path)] = "dir"
        else:
            state[str(path)] = (hashlib.sha1(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return state


def file_state(path: Path):
    return hashlib.sha1(Path(path).read_bytes()).hexdigest(), Path(path).stat().st_mtime_ns


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class TestRunDirs(TempDirCase):
    def test_new_run_dirs_are_unique_and_empty(self):
        now = datetime(2026, 10, 13, 9, 30, 0)
        first = pipeline.new_run_dir(self.tmp / "runs", now=now)
        second = pipeline.new_run_dir(self.tmp / "runs", now=now)
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith("20261013T093000-"))
        self.assertEqual((list(first.iterdir()), list(second.iterdir())), ([], []))

    def test_name_collision_never_reuses_a_folder(self):
        runs = self.tmp / "runs"
        existing = pipeline.new_run_dir(runs, now=datetime(2026, 10, 13))
        (existing / "result.json").write_text("{}", encoding="utf-8")
        suffix = existing.name.split("-")[1]
        fixed = [mock.Mock(hex=suffix + "0" * 26), mock.Mock(hex="abcdef" + "0" * 26)]
        with mock.patch.object(pipeline.uuid, "uuid4", side_effect=fixed):
            fresh = pipeline.new_run_dir(runs, now=datetime(2026, 10, 13))
        self.assertEqual(fresh.name, "20261013T000000-abcdef")
        self.assertEqual((existing / "result.json").read_text(encoding="utf-8"), "{}")

    def test_prepare_run_dir_refuses_an_earlier_runs_files(self):
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        (run_dir / "run.log").write_text("earlier", encoding="utf-8")
        with self.assertRaises(ValueError):
            pipeline.prepare_run_dir(run_dir)
        self.assertEqual((run_dir / "run.log").read_text(encoding="utf-8"), "earlier")


class TestStreamingAgentRunner(TempDirCase):
    def test_stage_stdout_and_stderr_reach_the_log_and_the_exit_code_is_kept(self):
        code = "import sys; print('stage says hi'); sys.stderr.write('stage error line\\n'); sys.exit(3)"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = pipeline.streaming_agent_runner([sys.executable, "-c", code], cwd=str(self.tmp),
                                                     env=dict(os.environ), timeout=60, check=False)
        self.assertEqual(result.returncode, 3)
        self.assertIn("stage says hi", buf.getvalue())
        self.assertIn("stage error line", buf.getvalue())

    def test_timeout_kills_the_stage_and_raises(self):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(subprocess.TimeoutExpired):
            pipeline.streaming_agent_runner([sys.executable, "-c", "import time; time.sleep(30)"],
                                            cwd=str(self.tmp), env=dict(os.environ), timeout=0.5)

    def test_windows_line_endings_are_not_doubled_in_run_log(self):
        """A child writing "\\r\\n" (any Windows Python stage) must give one line ending per line in
        run.log, not "\\r\\r\\n"; invalid UTF-8 is replaced, never fatal."""
        child = ("import sys; sys.stdout.buffer.write(b'first line\\r\\nsecond \\xff bad\\r\\nthird\\r\\n'); "
                 "sys.stdout.flush()")
        run_dir = pipeline.prepare_run_dir(self.tmp / "run")
        recorder = pipeline.RunRecorder(run_dir, "cli", {}, pipeline.PipelineSettings())
        console = io.StringIO()
        with contextlib.redirect_stdout(console), contextlib.redirect_stderr(io.StringIO()):
            recorder.start()  # the real run.log: text mode, UTF-8, platform line endings
            try:
                result = pipeline.streaming_agent_runner([sys.executable, "-c", child], cwd=str(self.tmp),
                                                         env=dict(os.environ), timeout=60)
            finally:
                recorder.close()
        self.assertEqual(result.returncode, 0)
        expected_text = "first line\nsecond � bad\nthird\n"
        self.assertTrue(console.getvalue().endswith(expected_text))
        raw = (run_dir / "run.log").read_bytes()
        stage_bytes = raw[raw.index(b"first line"):]
        self.assertEqual(stage_bytes, expected_text.replace("\n", os.linesep).encode("utf-8"))
        self.assertNotIn(b"\r\r", raw)
        self.assertEqual((run_dir / "run.log").read_text(encoding="utf-8").splitlines()[-3:],
                         ["first line", "second � bad", "third"])

    def test_logged_agent_stages_keeps_run_agent_stages_and_its_scope(self):
        with mock.patch.object(pipeline, "run_agent_stages", return_value=AgentRunResult(True)) as stages:
            pipeline.logged_agent_stages("agent", ["score_jobs.py"], "export.json", None, 5, scope_path="scope.json")
        args, kwargs = stages.call_args
        self.assertEqual(args, ("agent", ["score_jobs.py"], "export.json", None, 5))
        self.assertEqual(kwargs, {"scope_path": "scope.json", "runner": pipeline.streaming_agent_runner})


class TestWorkflowGuard(unittest.TestCase):
    def args(self, **overrides):
        values = dict(no_apply=True, sync_only=False)
        values.update(overrides)
        return Namespace(**values)

    def settings(self, **overrides):
        values = dict(mode="SAFE", sheets_enabled=False, export_limit=2, window_hours=24,
                      agent_stages=["scrape_jobs.py", "score_jobs.py"])
        values.update(overrides)
        return pipeline.PipelineSettings(**values)

    def guard(self, workflow="scan-and-score", env=None, args=None, keys=frozenset(), **settings):
        return pipeline.workflow_guard(workflow, args or self.args(), self.settings(**settings),
                                       env=PREP_ENV if env is None else env, project_env_keys=set(keys))

    def test_preparation_settings_pass(self):
        self.assertEqual(self.guard(), [])
        self.assertEqual(self.guard("full-preparation",
                                    agent_stages=["scrape_jobs.py", "score_jobs.py", "tailor_job.py"]), [])

    def test_own_env_keys_reads_names_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("GEMINI_API_KEY=secret-value\nartifact_sync=1\n", encoding="utf-8")
            keys = pipeline.own_env_keys(env_file)
            self.assertEqual(keys, {"GEMINI_API_KEY", "artifact_sync"})
            self.assertEqual(pipeline.own_env_keys(Path(tmp) / "missing.env"), set())

    def test_cli_workflow_has_no_extra_rules(self):
        self.assertEqual(pipeline.workflow_guard("cli", self.args(no_apply=False), self.settings(mode="AUTO"),
                                                 env={}), [])

    def test_every_unsafe_setting_is_refused(self):
        cases = [
            dict(args=self.args(no_apply=False)),
            dict(args=self.args(sync_only=True)),
            dict(mode="AUTO"),
            dict(sheets_enabled=True),
            dict(env={}),
            dict(env={"artifact_sync": "1"}),
            dict(export_limit=5),
            dict(window_hours=48),
            dict(agent_stages=["scrape_jobs.py", "company_research.py"]),
            dict(agent_stages=["scrape_jobs.py", "score_jobs.py", "tailor_job.py"]),  # scan-and-score tailors
            dict(keys={"artifact_sync"}),  # the bridge would drop artifact_sync=0 before the Agent starts
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertTrue(self.guard(**case))


class RecordedRunCase(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.factory_kwargs = {}

    def prep_settings(self, **overrides):
        values = dict(sheets_enabled=False, export_limit=2, agent_stages=["scrape_jobs.py", "score_jobs.py"])
        values.update(overrides)
        return self.settings(**values)

    def run_main(self, argv, settings=None, scanner=None, agent=None, env=PREP_ENV):
        def factory(settings, **kwargs):
            self.factory_kwargs = dict(kwargs)
            kwargs.pop("agent_runner", None)  # the fake Agent below instead of a real subprocess
            return pipeline.ApplicationPipeline(
                settings, scanner_factory=lambda: scanner or FakeScanner(self), browser_factory=lambda: self.browser,
                runner_factory=self.make_runner, agent_runner=agent or self.scoring_agent,
                tracker_factory=lambda *a: self.fail("Sheets must not be used"), prompt=self.no_prompt,
                out=print, **kwargs)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(pipeline, "own_env_keys", return_value=set()),                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pipeline.main(argv, settings=settings or self.prep_settings(), pipeline_factory=factory)
        return code, out.getvalue()

    def no_prompt(self, message):
        self.fail(f"a menu workflow must never prompt: {message}")

    def scoring_agent(self, agent_dir, stages, export_path, python, timeout, scope_path=None):
        exported = json.loads(Path(export_path).read_text(encoding="utf-8"))
        self.agent_calls.append(exported)
        self.agent_scopes.append(scope_path)
        scored = [dict(e, score=9, reasoning="fit", matched_must_haves=["Java"], missing_must_haves=[])
                  for e in exported]
        (self.agent_out / "scored_jobs.json").write_text(json.dumps(scored), encoding="utf-8")
        self.write_agent_report(scope_path, score={"newly_scored": [e["source_job_id"] for e in exported]})
        return AgentRunResult(True, list(stages))

    def report(self, run_dir):
        return json.loads((run_dir / "result.json").read_text(encoding="utf-8"))


class TestRecordedRuns(RecordedRunCase):
    def test_scan_and_score_run_writes_log_and_complete_report(self):
        run_dir = self.tmp / "runs" / "20261013T090000-aaaaaa"
        code, console = self.run_main(["--no-apply", "--workflow", "scan-and-score", "--run-dir", str(run_dir)],
                                      scanner=FakeScanner(self, jobs=["900000000001"]))
        report = self.report(run_dir)
        scope = json.loads(Path(self.agent_scopes[0]).read_text(encoding="utf-8"))
        self.assertEqual(code, report["exit_code"])
        self.assertEqual(report["status"], {0: "complete", 2: "attention"}[code])
        self.assertEqual(report["exit_meaning"], pipeline.exit_meaning(code))
        self.assertEqual((report["run_id"], report["workflow"], report["application_mode"], report["no_apply"]),
                         (run_dir.name, "scan-and-score", "SAFE", True))
        for key in ("started_at", "finished_at"):
            self.assertIsNotNone(datetime.fromisoformat(report[key]).tzinfo)
        self.assertGreaterEqual(report["duration_seconds"], 0)
        self.assertEqual(report["agent_export_id"], scope["run_id"])
        self.assertEqual(len(report["exported_job_ids"]), 1)
        self.assertEqual(report["effective_settings"]["artifact_sync"], "0")
        self.assertEqual(report["effective_settings"]["agent_stages"], ["scrape_jobs.py", "score_jobs.py"])
        self.assertFalse(report["effective_settings"]["google_sheets_enabled"])
        self.assertEqual(report["stages"]["applications"], "SKIPPED (--no-apply)")
        self.assertEqual(set(report["counts"]["applications"].values()), {None})  # unknown, not 0
        self.assertEqual(report["counts"]["export"]["exported"], 1)
        self.assertEqual(report["counts"]["scan"]["new_jobs"], 1)
        self.assertEqual(report["options"]["no_apply"], True)
        self.assertNotIn("GEMINI_API_KEY", json.dumps(report))
        log = (run_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("NAUKRI AUTO JOB APPLICATION", log)
        self.assertIn(f"{'Agent export ID:':<38}{scope['run_id']}", log)
        self.assertIn(str(run_dir / "result.json"), console)
        self.assertEqual(self.browser.clicks, 0)
        self.assertIs(self.factory_kwargs["agent_runner"], pipeline.logged_agent_stages)
        self.assertFalse(Path(str(run_dir / "result.json") + ".tmp").exists())

    def test_failed_agent_stage_is_never_reported_as_success(self):
        failing = lambda *a, **k: AgentRunResult(False, failed_stage="score_jobs.py",  # noqa: E731
                                                 code="AGENT_STAGE_FAILED", message="score_jobs.py exited with code 1")
        run_dir = self.tmp / "run"
        code, _ = self.run_main(["--no-apply", "--workflow", "scan-and-score", "--run-dir", str(run_dir)],
                                scanner=FakeScanner(self, jobs=["900000000001"]), agent=failing)
        report = self.report(run_dir)
        self.assertEqual((code, report["exit_code"], report["status"]), (2, 2, "attention"))
        self.assertIn("AGENT_STAGE_FAILED", report["stages"]["ai_agent"])
        self.assertTrue(any("AGENT_STAGE_FAILED" in w for w in report["warnings"]))
        self.assertIn("RUN COMPLETE (ATTENTION NEEDED)", (run_dir / "run.log").read_text(encoding="utf-8"))

    def test_crash_is_logged_with_traceback_and_recorded(self):
        def exploding(*a, **k):
            raise RuntimeError("agent exploded")

        run_dir = self.tmp / "run"
        with self.assertRaises(RuntimeError):
            self.run_main(["--no-apply", "--workflow", "scan-and-score", "--run-dir", str(run_dir)],
                          scanner=FakeScanner(self, jobs=["900000000001"]), agent=exploding)
        report = self.report(run_dir)
        self.assertEqual((report["status"], report["exit_code"]), ("crashed", 1))
        self.assertEqual(report["error"], "RuntimeError: agent exploded")
        self.assertIsNone(report["counts"])  # partial run: unknown, not zeros
        self.assertIsNone(report["stages"])
        self.assertIsNotNone(report["agent_export_id"])  # the export happened before the crash
        log = (run_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("Traceback", log)
        self.assertIn("agent exploded", log)

    def test_interrupt_is_recorded(self):
        def interrupted(*a, **k):
            raise KeyboardInterrupt

        run_dir = self.tmp / "run"
        code, _ = self.run_main(["--no-apply", "--workflow", "scan-and-score", "--run-dir", str(run_dir)],
                                scanner=FakeScanner(self, jobs=["900000000001"]), agent=interrupted)
        report = self.report(run_dir)
        self.assertEqual((code, report["status"], report["exit_code"]), (2, "interrupted", 2))
        self.assertIsNone(report["counts"])

    def test_configuration_failure_marks_later_steps_not_run(self):
        run_dir = self.tmp / "run"
        code, _ = self.run_main(["--no-apply", "--run-dir", str(run_dir)],
                                settings=self.prep_settings(chrome_profile=self.tmp / "missing"))
        report = self.report(run_dir)
        self.assertEqual((code, report["status"], report["exit_code"]), (1, "failed", 1))
        self.assertEqual(report["stages"]["configuration"], "FAILED")
        self.assertTrue(report["stages"]["scan"].startswith("NOT RUN"))
        self.assertIsNone(report["counts"]["scan"]["jobs_scanned"])
        self.assertIsNone(report["exported_job_ids"])
        self.assertIn("Naukri browser profile", report["fatal"])

    def test_workflow_guard_refuses_before_anything_runs(self):
        run_dir = self.tmp / "run"
        scanner = FakeScanner(self, jobs=["900000000001"])
        code, console = self.run_main(["--workflow", "scan-and-score", "--run-dir", str(run_dir)], scanner=scanner)
        report = self.report(run_dir)
        self.assertEqual((code, report["status"]), (1, "failed"))
        self.assertIn("--no-apply is required", report["fatal"])
        self.assertEqual((self.agent_calls, self.browser.clicks, self.factory_kwargs), ([], 0, {}))
        self.assertIn("Nothing was run", console)

    def test_preparation_workflow_refused_without_artifact_sync_off(self):
        run_dir = self.tmp / "run"
        code, _ = self.run_main(["--no-apply", "--workflow", "full-preparation", "--run-dir", str(run_dir)], env={})
        self.assertEqual(code, 1)
        self.assertIn("artifact_sync", self.report(run_dir)["fatal"])

    def test_existing_report_is_never_overwritten(self):
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        (run_dir / "result.json").write_text('{"keep": true}', encoding="utf-8")
        code, _ = self.run_main(["--no-apply", "--run-dir", str(run_dir)])
        self.assertEqual(code, 1)
        self.assertEqual((run_dir / "result.json").read_text(encoding="utf-8"), '{"keep": true}')
        self.assertFalse((run_dir / "run.log").exists())

    def test_menu_workflow_requires_a_run_dir(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.main(["--no-apply", "--workflow", "scan-and-score"], settings=self.prep_settings())

    def test_dry_run_records_no_export_id_and_writes_nothing(self):
        self.add_job("900000000001", scored=False, posted_label="2 hours ago")
        run_dir = self.tmp / "run"
        code, _ = self.run_main(["--dry-run", "--no-apply", "--skip-scan", "--run-dir", str(run_dir)])
        report = self.report(run_dir)
        self.assertEqual((report["agent_export_id"], report["exported_job_ids"]), (None, None))
        self.assertEqual((self.agent_calls, self.browser.clicks), ([], 0))
        self.assertEqual(db.get_sheet_syncs(), [])

    def test_summary_shows_the_export_id(self):
        summary = pipeline.RunSummary(agent_export_id="20261013T090000-abcd1234")
        self.assertIn(f"{'Agent export ID:':<38}20261013T090000-abcd1234",
                      pipeline.format_summary(summary))
        self.assertIn(f"{'Agent export ID:':<38}-", pipeline.format_summary(pipeline.RunSummary()))


class LoginBrowser:
    """Session check says login_required; wait_for_login records the call and returns `logs_in`."""

    def __init__(self, logs_in):
        self.logs_in, self.waits, self.clicks = logs_in, [], 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def session_state(self):
        return "login_required"

    def wait_for_login(self, minutes, out=print):
        self.waits.append(minutes)
        return self.logs_in


class TestWaitForLogin(RecordedRunCase):
    def run_with(self, browser, argv_extra=()):
        self.browser = browser
        run_dir = self.tmp / "run"
        code, console = self.run_main(["--no-apply", "--workflow", "scan-and-score", "--run-dir", str(run_dir),
                                       *argv_extra], scanner=FakeScanner(self, jobs=["900000000001"]))
        return code, self.report(run_dir), console

    def test_manual_login_then_the_scan_continues(self):
        browser = LoginBrowser(logs_in=True)
        code, report, console = self.run_with(browser, ["--wait-for-login"])
        self.assertEqual(browser.waits, [pipeline.NAUKRI_LOGIN_WAIT_MINUTES])  # once, never retried
        self.assertEqual(report["stages"]["naukri_session"], "OK")
        self.assertEqual(report["stages"]["scan"], "OK")
        self.assertTrue(report["options"]["wait_for_login"])
        self.assertIn("Nothing is typed for you", console)
        self.assertEqual(browser.clicks, 0)

    def test_login_not_completed_is_reported_not_retried(self):
        browser = LoginBrowser(logs_in=False)
        code, report, _ = self.run_with(browser, ["--wait-for-login"])
        self.assertEqual(browser.waits, [pipeline.NAUKRI_LOGIN_WAIT_MINUTES])
        self.assertEqual((report["stages"]["naukri_session"], code, report["status"]), ("AUTH_REQUIRED", 2, "attention"))

    def test_without_the_flag_nothing_waits(self):
        browser = LoginBrowser(logs_in=True)
        _, report, _ = self.run_with(browser)
        self.assertEqual(browser.waits, [])
        self.assertEqual(report["stages"]["naukri_session"], "AUTH_REQUIRED")
        self.assertFalse(report["options"]["wait_for_login"])


class TestLastRun(TempDirCase):
    def write_report(self, name, **fields):
        folder = self.tmp / "runs" / name
        folder.mkdir(parents=True)
        report = {"run_id": name, "status": "complete", "started_at": "2026-10-13T09:00:00+05:30",
                  "finished_at": "2026-10-13T09:05:00+05:30", "duration_seconds": 300.0,
                  "workflow": "scan-and-score", "application_mode": "SAFE", "no_apply": True, "exit_code": 0,
                  "agent_export_id": "20261013T090001-abcd1234", "exported_job_ids": [7, 8],
                  "stages": {"scan": "OK"}, "warnings": [], "summary_text": "SUMMARY TEXT",
                  "log_file": str(folder / "run.log")}
        report.update(fields)
        (folder / "result.json").write_text(json.dumps(report), encoding="utf-8")
        return folder

    def show(self):
        lines = []
        code = pipeline.show_last_run(self.tmp / "runs", out=lines.append)
        return code, "\n".join(lines)

    def test_no_runs(self):
        code, text = self.show()
        self.assertEqual(code, 0)
        self.assertIn("No previous runs found", text)
        self.assertFalse((self.tmp / "runs").exists())

    def test_newest_valid_report(self):
        self.write_report("20261012T090000-aaaaaa", agent_export_id="old")
        folder = self.write_report("20261013T090000-bbbbbb")
        code, text = self.show()
        self.assertEqual(code, 0)
        for expected in ("20261013T090000-bbbbbb", "scan-and-score", "SAFE (--no-apply)", "0 (run complete)",
                         "20261013T090001-abcd1234", "[7, 8]", "SUMMARY TEXT", str(folder / "result.json")):
            self.assertIn(expected, text)
        self.assertNotIn("old", text)

    def test_malformed_newest_is_skipped_and_named(self):
        self.write_report("20261012T090000-aaaaaa")
        broken = self.tmp / "runs" / "20261013T090000-bbbbbb"
        broken.mkdir()
        (broken / "result.json").write_text("{not json", encoding="utf-8")
        missing = self.tmp / "runs" / "20261013T100000-cccccc"
        missing.mkdir()
        code, text = self.show()
        self.assertEqual(code, 0)
        self.assertIn("skipped newer folder 20261013T100000-cccccc: no result.json", text)
        self.assertIn("skipped newer folder 20261013T090000-bbbbbb: not valid JSON", text)
        self.assertIn("Run ID:               20261012T090000-aaaaaa", text)

    def test_incomplete_report_is_shown_as_unfinished(self):
        self.write_report("20261013T090000-bbbbbb", status="running", exit_code=None, finished_at=None,
                          duration_seconds=None, agent_export_id=None, exported_job_ids=None, summary_text=None)
        code, text = self.show()
        self.assertEqual(code, 0)
        self.assertIn("never finished its report", text)
        self.assertIn("not recorded (unknown: the run ended without a final exit code)", text)
        self.assertIn("Exported job IDs:     unknown", text)

    def test_reports_missing_required_fields_or_not_objects_are_malformed(self):
        self.write_report("20261013T090000-bbbbbb", run_id="")
        other = self.tmp / "runs" / "20261013T100000-cccccc"
        other.mkdir()
        (other / "result.json").write_text("[1, 2]", encoding="utf-8")
        code, text = self.show()
        self.assertEqual(code, 1)
        self.assertIn("missing run_id", text)
        self.assertIn("not a JSON object", text)

    def test_last_run_is_read_only(self):
        self.write_report("20261012T090000-aaaaaa")
        broken = self.tmp / "runs" / "20261013T090000-bbbbbb"
        broken.mkdir()
        (broken / "result.json").write_text("{", encoding="utf-8")
        before = snapshot(self.tmp)
        with mock.patch.object(pipeline, "RUNS_DIR", self.tmp / "runs"), \
                mock.patch.object(pipeline, "init_db", side_effect=AssertionError("init_db called")), \
                mock.patch.object(pipeline, "ApplicationPipeline", side_effect=AssertionError("pipeline started")), \
                mock.patch.object(pipeline.subprocess, "Popen", side_effect=AssertionError("process started")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = pipeline.main(["--last-run"])
        self.assertEqual(code, 0)
        self.assertIn("20261012T090000-aaaaaa", out.getvalue())
        self.assertEqual(snapshot(self.tmp), before)


class TestReadOnlyConfigCheck(PipelineTestCase):
    def check(self, **overrides):
        with mock.patch.object(pipeline, "init_db", side_effect=AssertionError("init_db called")):
            checks = pipeline.validate_configuration(self.settings(**overrides), initialize_db=False)
        return {c.name: c for c in checks}

    def test_existing_database_is_not_modified(self):
        path = Path(db.DB_PATH)
        before = file_state(path)
        sibling_files = sorted(p.name for p in path.parent.iterdir())
        database = self.check()["SQLite database"]
        self.assertTrue(database.ok)
        self.assertIn("read-only check, 0 confirmed application(s)", database.detail)
        self.assertEqual(file_state(path), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), sibling_files)  # no journal left

    def test_missing_database_is_not_created(self):
        missing = self.tmp / "new" / "naukri.db"
        with mock.patch.object(db, "DB_PATH", missing):
            database = self.check()["SQLite database"]
        self.assertFalse(missing.exists())
        self.assertFalse(missing.parent.exists())
        self.assertFalse(database.ok)
        self.assertFalse(database.fatal)
        self.assertIn("not created yet", database.detail)

    def test_pending_legacy_migration_is_not_performed(self):
        target, legacy = self.tmp / "data" / "naukri.db", self.tmp / "jobs_legacy.db"
        legacy.write_bytes(Path(db.DB_PATH).read_bytes())
        before = file_state(legacy)
        with mock.patch.object(db, "DB_PATH", target), mock.patch.object(db, "DEFAULT_DB_PATH", target), \
                mock.patch.object(db, "LEGACY_DB_PATH", legacy):
            database = self.check()["SQLite database"]
        self.assertFalse(target.exists())
        self.assertEqual(file_state(legacy), before)
        self.assertIn("copies the legacy jobs.db", database.detail)

    def test_missing_tables_are_reported_not_created(self):
        partial = self.tmp / "partial.db"
        with sqlite3.connect(partial) as conn:
            conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
        before = file_state(partial)
        with mock.patch.object(db, "DB_PATH", partial):
            database = self.check()["SQLite database"]
            tables = db.inspect_database_readonly()["tables"]
        self.assertEqual(tables, ["jobs"])
        self.assertFalse(database.fatal)
        self.assertIn("applied_jobs", database.detail)
        self.assertEqual(file_state(partial), before)

    def test_unreadable_database_is_fatal_and_untouched(self):
        broken = self.tmp / "broken.db"
        broken.write_bytes(b"this is not a sqlite database" * 100)
        before = file_state(broken)
        with mock.patch.object(db, "DB_PATH", broken):
            database = self.check()["SQLite database"]
        self.assertFalse(database.ok)
        self.assertTrue(database.fatal)
        self.assertEqual(file_state(broken), before)

    def test_confirmed_applications_are_counted_read_only(self):
        job = self.fresh_job()
        db.record_application_attempt(job, "applied")
        info = db.inspect_database_readonly()
        self.assertEqual((info["confirmed_applications"], info["missing_tables"], info["error"]), (1, [], None))

    def test_check_config_cli_is_read_only(self):
        path = Path(db.DB_PATH)
        before = file_state(path)
        with mock.patch.object(pipeline, "init_db", side_effect=AssertionError("init_db called")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = pipeline.main(["--check-config"], settings=self.settings())
        self.assertEqual(code, 0)
        self.assertIn("read-only check", out.getvalue())
        self.assertEqual(file_state(path), before)

    def test_pipeline_runs_still_initialise_the_database(self):
        with mock.patch.object(pipeline, "init_db") as init:
            pipeline.validate_configuration(self.settings())
        init.assert_called_once()


if __name__ == "__main__":
    unittest.main()
