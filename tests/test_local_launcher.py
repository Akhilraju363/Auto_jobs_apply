"""start_local.bat's menu (local_launcher.py): choices, confirmations, enforced settings, blocking, lock.

The pipeline is never started: `launch` is a fake that records the arguments and environment it would
have used. Agent folders are temporary fakes. Nothing here scans, scores, uploads or applies.
"""

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import local_launcher as launcher

TAILORING_STAGES = ["scrape_jobs.py", "score_jobs.py", "tailor_job.py"]
HOSTILE_ENV = {"APPLICATION_MODE": "AUTO", "artifact_sync": "1", "GOOGLE_SHEETS_ENABLED": "true",
               "AI_AGENT_EXPORT_LIMIT": "10", "APPLICATION_JOB_WINDOW_HOURS": "72", "JOB_SOURCES": "linkedin,naukri",
               "AI_AGENT_STAGES": "scrape_jobs.py,score_jobs.py,tailor_job.py,company_research.py"}


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.runs = self.tmp / "runs"


class TestWorkflows(unittest.TestCase):
    def test_scan_and_score_never_tailors_and_is_no_apply(self):
        workflow = launcher.build_workflow("2", TAILORING_STAGES)  # configured stages are ignored for option 2
        self.assertEqual(workflow.args, ("--no-apply", "--workflow", "scan-and-score"))
        self.assertEqual(workflow.env, {"AI_AGENT_STAGES": "scrape_jobs.py,score_jobs.py"})
        self.assertNotIn("tailor_job.py", workflow.env["AI_AGENT_STAGES"])
        self.assertEqual(workflow.confirmation, "y")

    def test_full_preparation_uses_the_configured_scoped_stages_and_needs_YES(self):
        workflow = launcher.build_workflow("3", TAILORING_STAGES)
        self.assertEqual(workflow.args, ("--no-apply", "--workflow", "full-preparation"))
        self.assertEqual(workflow.env, {"AI_AGENT_STAGES": "scrape_jobs.py,score_jobs.py,tailor_job.py"})
        self.assertEqual(workflow.confirmation, "YES")
        for words in ("LIVE Naukri scan", "SQLite database", "LLM", "UPLOADS THE RESUME PDF TO DRIVE",
                      "artifact_sync=0", "queued rows stay queued", "No application is submitted"):
            self.assertIn(words, workflow.warning)

    def test_unscoped_or_empty_stages_block_full_preparation(self):
        for stages in (["scrape_jobs.py", "company_research.py"], ["write_sheet.py"], [], [" "]):
            with self.subTest(stages=stages), self.assertRaises(launcher.WorkflowBlocked):
                launcher.build_workflow("3", stages)

    def test_only_options_2_and_3_are_workflows(self):
        for choice in ("1", "4", "5", "", "x"):
            with self.subTest(choice=choice), self.assertRaises(launcher.WorkflowBlocked):
                launcher.build_workflow(choice, TAILORING_STAGES)

    def test_enforced_settings_override_the_callers_environment(self):
        for choice in ("2", "3"):
            workflow = launcher.build_workflow(choice, TAILORING_STAGES)
            env = launcher.child_environment(workflow.env, base=dict(HOSTILE_ENV, KEEP_ME="1"))
            with self.subTest(choice=choice):
                self.assertEqual(env["APPLICATION_MODE"], "SAFE")
                self.assertEqual(env["artifact_sync"], "0")
                self.assertEqual(env["GOOGLE_SHEETS_ENABLED"], "false")
                self.assertEqual(env["AI_AGENT_EXPORT_LIMIT"], "2")
                self.assertEqual(env["APPLICATION_JOB_WINDOW_HOURS"], "24")
                self.assertEqual(env["JOB_SOURCES"], "naukri")
                self.assertEqual(env["AI_AGENT_STAGES"], workflow.env["AI_AGENT_STAGES"])
                self.assertEqual(env["KEEP_ME"], "1")

    def test_a_workflow_that_could_submit_is_refused(self):
        unsafe = [
            launcher.Workflow("2", "scan-and-score", "t", ("--workflow", "scan-and-score")),
            launcher.Workflow("2", "scan-and-score", "t", ("--no-apply", "--sync-only")),
            launcher.Workflow("2", "scan-and-score", "t", ("--no-apply",), {"APPLICATION_MODE": "AUTO"}),
            launcher.Workflow("2", "scan-and-score", "t", ("--no-apply",), {"GOOGLE_SHEETS_ENABLED": "true"}),
            launcher.Workflow("2", "scan-and-score", "t", ("--no-apply",), {"artifact_sync": "1"}),
        ]
        for workflow in unsafe:
            with self.subTest(workflow=workflow), self.assertRaises(launcher.WorkflowBlocked):
                launcher.ensure_cannot_submit(workflow)


class FakeAgent:
    """A temporary AI Agent checkout with the files preflight_checks looks for."""

    def __init__(self, root: Path, scripts=launcher.REQUIRED_AGENT_SCRIPTS, honours_artifact_sync=True):
        self.dir = root / "agent dir with spaces"
        (self.dir / "scripts").mkdir(parents=True)
        (self.dir / "output").mkdir()
        for name in scripts:
            (self.dir / "scripts" / name).write_text("# fake\n", encoding="utf-8")
        if "artifacts.py" in scripts:
            marker = launcher.ARTIFACT_SYNC_MARKER if honours_artifact_sync else "os.environ.get('other'"
            (self.dir / "scripts" / "artifacts.py").write_text(f'_DISABLED = {marker}, "")\n', encoding="utf-8")
        python = self.dir / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        self.project = root / "project"
        self.project.mkdir()
        (self.project / ".env").write_text("", encoding="utf-8")

    def checks(self, output=None, stages=TAILORING_STAGES):
        return launcher.preflight_checks(self.dir, output if output is not None else self.dir / "output", None,
                                         stages, project_dir=self.project)


class TestPreflight(TempDirCase):
    def test_complete_setup_blocks_nothing(self):
        checks = FakeAgent(self.tmp).checks()
        self.assertEqual(launcher.blocked_choices(checks), {}, [c for c in checks if not c.ok])

    def test_wrong_output_dir_blocks_live_options(self):
        agent = FakeAgent(self.tmp)
        other = self.tmp / "elsewhere" / "output"
        other.mkdir(parents=True)
        blocked = launcher.blocked_choices(agent.checks(output=other))
        self.assertEqual(sorted(blocked), ["2", "3", "auto"])
        self.assertIn("AI_AGENT_OUTPUT_DIR", blocked["2"][0])

    def test_not_the_agent_checkout_blocks_live_options(self):
        agent = FakeAgent(self.tmp, scripts=("scrape_jobs.py", "score_jobs.py"))
        blocked = launcher.blocked_choices(agent.checks())
        self.assertEqual(sorted(blocked), ["2", "3", "auto"])
        self.assertTrue(any("job_scope.py" in reason for reason in blocked["2"]))

    def test_unset_agent_paths_block_live_options(self):
        checks = launcher.preflight_checks(None, None, None, TAILORING_STAGES, project_dir=self.tmp)
        self.assertEqual(sorted(launcher.blocked_choices(checks)), ["2", "3", "auto"])

    def test_agent_without_artifact_sync_switch_blocks_live_options(self):
        blocked = launcher.blocked_choices(FakeAgent(self.tmp, honours_artifact_sync=False).checks())
        self.assertEqual(sorted(blocked), ["2", "3", "auto"])
        self.assertTrue(any("artifact_sync" in reason for reason in blocked["2"]))

    def test_artifact_sync_in_this_projects_env_blocks_live_options(self):
        agent = FakeAgent(self.tmp)
        (agent.project / ".env").write_text("artifact_sync=0\n", encoding="utf-8")  # dropped by the bridge
        blocked = launcher.blocked_choices(agent.checks())
        self.assertEqual(sorted(blocked), ["2", "3", "auto"])
        self.assertTrue(any("artifact_sync is defined" in reason for reason in blocked["2"]))

    def test_unscoped_configured_stage_blocks_only_full_preparation(self):
        checks = FakeAgent(self.tmp).checks(stages=["scrape_jobs.py", "score_jobs.py", "company_research.py"])
        blocked = launcher.blocked_choices(checks)
        self.assertEqual(list(blocked), ["3"])


class FakeLaunch:
    """Stands in for launch_pipeline: records each call; optionally writes the run's report."""

    def __init__(self, code=0, write_report=True, report_exit=None):
        self.calls = []
        self.code, self.write_report, self.report_exit = code, write_report, report_exit

    def __call__(self, args, env):
        self.calls.append((list(args), dict(env)))
        if "--run-dir" in args and self.write_report:
            run_dir = Path(args[args.index("--run-dir") + 1])
            exit_code = self.code if self.report_exit is None else self.report_exit
            report = {"run_id": run_dir.name, "status": "complete" if exit_code == 0 else "attention",
                      "started_at": "2026-10-13T09:00:00+05:30", "exit_code": exit_code,
                      "agent_export_id": "20261013T090001-abcd1234"}
            (run_dir / "result.json").write_text(json.dumps(report), encoding="utf-8")
        return self.code

    def pipeline_runs(self):
        return [(args, env) for args, env in self.calls if "--run-dir" in args]


class TestMenu(TempDirCase):
    def menu(self, answers, checks=(), launch=None, stages=TAILORING_STAGES):
        replies = list(answers)
        self.prompts, self.output = [], []
        self.launch = launch or FakeLaunch()

        def ask(prompt):
            self.prompts.append(prompt)
            if not replies:
                raise EOFError
            return replies.pop(0)

        code = launcher.Launcher(list(checks), list(stages), self.runs, input_fn=ask, out=self.output.append,
                                 launch=self.launch).run()
        return code, "\n".join(self.output)

    def test_exit_runs_nothing(self):
        code, text = self.menu(["5"])
        self.assertEqual((code, self.launch.calls), (0, []))
        self.assertIn("nothing was run", text)
        self.assertFalse(self.runs.exists())

    def test_enter_and_invalid_input_never_select_an_option(self):
        code, text = self.menu(["", "  ", "0", "6", "2x", "yes", "YES", "5"])
        self.assertEqual((code, self.launch.calls), (0, []))
        self.assertEqual(text.count("Invalid choice"), 7)
        self.assertFalse(self.runs.exists())

    def test_end_of_input_exits_without_running(self):
        code, text = self.menu([])
        self.assertEqual((code, self.launch.calls), (0, []))
        self.assertIn("exiting without running anything", text)

    def test_full_preparation_requires_exactly_YES(self):
        for answer in ("", "y", "yes", "Yes", "YES ", " YES", "no"):
            with self.subTest(answer=answer):
                self.menu(["3", answer, "", "5"])
                self.assertEqual(self.launch.calls, [])
                self.assertIn("Cancelled; nothing was run.", self.output)
        self.assertFalse(self.runs.exists())

    def test_full_preparation_with_YES_runs_once_with_enforced_settings(self):
        code, text = self.menu(["3", "YES", "", "5"])
        [(args, env)] = self.launch.pipeline_runs()
        self.assertEqual(args[:3], ["--no-apply", "--workflow", "full-preparation"])
        run_dir = Path(args[args.index("--run-dir") + 1])
        self.assertEqual(run_dir.parent, self.runs)
        self.assertEqual((env["APPLICATION_MODE"], env["artifact_sync"], env["GOOGLE_SHEETS_ENABLED"]),
                         ("SAFE", "0", "false"))
        self.assertEqual(env["AI_AGENT_STAGES"], "scrape_jobs.py,score_jobs.py,tailor_job.py")
        self.assertEqual(code, 0)
        for expected in ("Exit code:        0 (run complete)", f"Run ID:           {run_dir.name}",
                         "Agent export ID:  20261013T090001-abcd1234", str(run_dir / "result.json"),
                         str(run_dir / "run.log"), "RUN COMPLETE"):
            self.assertIn(expected, text)
        self.assertIn("UPLOADS THE RESUME PDF TO DRIVE", text)  # the warning came before the confirmation

    def test_scan_and_score_needs_y_and_runs_without_tailoring(self):
        self.menu(["2", "n", "", "5"])
        self.assertEqual(self.launch.calls, [])
        self.menu(["2", "y", "", "5"])
        [(args, env)] = self.launch.pipeline_runs()
        self.assertEqual(args[:3], ["--no-apply", "--workflow", "scan-and-score"])
        self.assertEqual(env["AI_AGENT_STAGES"], "scrape_jobs.py,score_jobs.py")
        self.assertEqual((env["artifact_sync"], env["GOOGLE_SHEETS_ENABLED"], env["APPLICATION_MODE"]),
                         ("0", "false", "SAFE"))

    def test_failed_run_shows_the_real_exit_code_and_is_not_called_complete(self):
        code, text = self.menu(["2", "y", "", "5"], launch=FakeLaunch(code=2))
        self.assertEqual(code, 2)
        self.assertIn("Exit code:        2 (run finished but needs your attention", text)
        self.assertIn("RUN DID NOT COMPLETE SUCCESSFULLY", text)
        self.assertNotIn("\nRUN COMPLETE\n", "\n" + text + "\n")

    def test_missing_report_is_called_out(self):
        code, text = self.menu(["2", "y", "", "5"], launch=FakeLaunch(code=1, write_report=False))
        self.assertEqual(code, 1)
        self.assertIn("NOT WRITTEN (no result.json): treat this run as failed", text)
        self.assertIn("RUN DID NOT COMPLETE SUCCESSFULLY", text)

    def test_report_disagreeing_with_the_process_is_called_out(self):
        _, text = self.menu(["2", "y", "", "5"], launch=FakeLaunch(code=1, report_exit=0))
        self.assertIn("the process exit code is authoritative", text)
        self.assertIn("RUN DID NOT COMPLETE SUCCESSFULLY", text)

    def test_config_check_and_last_run_are_not_pipeline_runs(self):
        self.menu(["1", "", "4", "", "5"])
        self.assertEqual([args for args, _ in self.launch.calls], [["--check-config"], ["--last-run"]])
        for _, env in self.launch.calls:
            self.assertEqual(env["APPLICATION_MODE"], "SAFE")
        self.assertFalse(self.runs.exists())  # no run folder, no lock file

    def test_failed_launcher_check_fails_the_config_check(self):
        failing = [launcher.Check("AI_AGENT_DIR", False, "not set in .env", ("2", "3"))]
        code, text = self.menu(["1", "", "5"], checks=failing)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] AI_AGENT_DIR", text)

    def test_blocked_option_never_asks_or_runs(self):
        failing = [launcher.Check("AI_AGENT_OUTPUT_DIR", False, "X is not Y", ("2", "3"))]
        code, text = self.menu(["2", "", "3", "", "5"], checks=failing)
        self.assertEqual((code, self.launch.calls), (1, []))
        self.assertEqual(text.count("is blocked; nothing was run"), 2)
        self.assertFalse(any("Type" in prompt for prompt in self.prompts))

    def test_overlapping_run_is_refused(self):
        lock = launcher.LauncherLock(self.runs / launcher.LOCK_NAME)
        self.assertTrue(lock.acquire())
        try:
            code, text = self.menu(["2", "y", "", "5"])
        finally:
            lock.release()
        self.assertEqual((code, self.launch.calls), (1, []))
        self.assertIn("Another launcher run is in progress", text)

    def test_no_menu_path_can_submit(self):
        """Every option, confirmed: each pipeline run is --no-apply and SAFE with Sheets and artifact sync off."""
        self.menu(["1", "", "2", "y", "", "3", "YES", "", "4", "", "5"])
        runs = self.launch.pipeline_runs()
        self.assertEqual(len(runs), 2)
        for args, env in self.launch.calls:
            self.assertNotIn("--sync-only", args)
            self.assertEqual(env["APPLICATION_MODE"], "SAFE")
        for args, env in runs:
            self.assertIn("--no-apply", args)
            self.assertEqual((env["GOOGLE_SHEETS_ENABLED"], env["artifact_sync"]), ("false", "0"))


OK_CHECKS = [launcher.Check("AI_AGENT_DIR", True, "fake agent")]
FAILED_CHECKS = [launcher.Check("AI_AGENT_OUTPUT_DIR", False, "X is not Y", ("2", "3"))]


class TestAutoRun(TempDirCase):
    """start_local.bat's default: scan and score only, no menu, no confirmation."""

    def auto(self, checks=OK_CHECKS, launch=None, config=(0, "CONFIGURATION\n  [OK  ] all good\n")):
        self.output, self.config_calls = [], []
        self.launch = launch or FakeLaunch()

        def check_config(env):
            self.config_calls.append(dict(env))
            return config

        code = launcher.AutoRun(list(checks), self.runs, out=self.output.append, launch=self.launch,
                                check_config=check_config).run()
        return code, "\n".join(self.output)

    def run_dirs(self):
        return sorted(p for p in self.runs.iterdir() if p.is_dir()) if self.runs.exists() else []

    def test_runs_scan_and_score_without_any_input(self):
        with unittest.mock.patch("builtins.input", side_effect=AssertionError("input requested")):
            code, text = self.auto()
        [(args, env)] = self.launch.pipeline_runs()
        run_dir = Path(args[args.index("--run-dir") + 1])
        self.assertEqual(args[:4], ["--no-apply", "--workflow", "scan-and-score", "--wait-for-login"])
        self.assertEqual(run_dir.parent, self.runs)
        self.assertEqual(code, 0)
        self.assertIn("Agent export ID:  20261013T090001-abcd1234", text)
        self.assertNotIn("Choose 1-5", text)  # no menu
        self.assertTrue((run_dir / launcher.LAUNCHER_LOG).is_file())

    def test_every_mandatory_setting_reaches_the_check_and_the_pipeline(self):
        with unittest.mock.patch.dict(launcher.os.environ, HOSTILE_ENV):
            self.auto()
        [(args, env)] = self.launch.pipeline_runs()
        expected = dict(launcher.SAFETY_ENV, AI_AGENT_STAGES="scrape_jobs.py,score_jobs.py")
        self.assertEqual(expected, {"APPLICATION_MODE": "SAFE", "artifact_sync": "0", "GOOGLE_SHEETS_ENABLED": "false",
                                    "JOB_SOURCES": "naukri", "AI_AGENT_EXPORT_LIMIT": "2",
                                    "APPLICATION_JOB_WINDOW_HOURS": "24",
                                    "AI_AGENT_STAGES": "scrape_jobs.py,score_jobs.py"})
        for passed in (env, self.config_calls[0]):
            self.assertEqual({k: passed[k] for k in expected}, expected)
        self.assertIn("--no-apply", args)
        self.assertNotIn("tailor_job.py", env["AI_AGENT_STAGES"])

    def test_failed_preflight_stops_before_the_pipeline_and_is_recorded(self):
        code, text = self.auto(checks=FAILED_CHECKS)
        self.assertEqual((code, self.launch.calls, self.config_calls), (1, [], []))
        [run_dir] = self.run_dirs()
        report = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((report["status"], report["exit_code"], report["workflow"]), ("failed", 1, "scan-and-score"))
        self.assertIn("AI_AGENT_OUTPUT_DIR: X is not Y", report["fatal"])
        self.assertEqual(report["stages"]["scan"], "NOT RUN (pre-flight failed)")
        self.assertIsNone(report["counts"])
        self.assertIsNone(report["agent_export_id"])
        self.assertIn("STOPPED before the scan", (run_dir / launcher.LAUNCHER_LOG).read_text(encoding="utf-8"))
        self.assertIn("RUN DID NOT COMPLETE SUCCESSFULLY", text)
        from application_pipeline import load_report
        self.assertIsNotNone(load_report(run_dir / "result.json")[0])  # --last-run can show it

    def test_failed_configuration_check_stops_before_the_pipeline(self):
        code, text = self.auto(config=(1, "CONFIGURATION\n  [FAIL] SQLite database  broken\n"))
        self.assertEqual((code, self.launch.calls), (1, []))
        [run_dir] = self.run_dirs()
        report = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((report["status"], report["exit_code"]), ("failed", 1))
        self.assertIn("configuration check failed", report["fatal"])
        self.assertIn("[FAIL] SQLite database", (run_dir / launcher.LAUNCHER_LOG).read_text(encoding="utf-8"))
        self.assertFalse((run_dir / "run.log").exists())

    def test_pipeline_failure_keeps_its_exit_code_and_report(self):
        code, text = self.auto(launch=FakeLaunch(code=2))
        self.assertEqual(code, 2)
        self.assertIn("RUN DID NOT COMPLETE SUCCESSFULLY", text)
        [run_dir] = self.run_dirs()
        self.assertNotIn("recorded_by", json.loads((run_dir / "result.json").read_text(encoding="utf-8")))

    def test_missing_pipeline_report_is_recorded_and_never_success(self):
        code, text = self.auto(launch=FakeLaunch(code=0, write_report=False))
        self.assertEqual(code, 1)
        [run_dir] = self.run_dirs()
        report = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((report["status"], report["recorded_by"], report["counts"]), ("crashed", "local_launcher", None))
        self.assertIn("RUN DID NOT COMPLETE SUCCESSFULLY", text)

    def test_lock_held_runs_nothing(self):
        lock = launcher.LauncherLock(self.runs / launcher.LOCK_NAME)
        self.assertTrue(lock.acquire())
        try:
            code, text = self.auto()
        finally:
            lock.release()
        self.assertEqual((code, self.launch.calls, self.config_calls, self.run_dirs()), (1, [], [], []))
        self.assertIn("Another launcher run is in progress", text)

    def test_each_attempt_gets_its_own_run_folder_and_nothing_is_retried(self):
        self.auto(launch=FakeLaunch(code=2))
        self.assertEqual(len(self.launch.calls), 1)  # one attempt, no retry
        self.auto()
        self.assertEqual(len(self.run_dirs()), 2)

    def test_main_refuses_arguments(self):
        with unittest.mock.patch.object(launcher, "AutoRun", side_effect=AssertionError("ran")), \
                unittest.mock.patch.object(launcher, "Launcher", side_effect=AssertionError("ran")):
            for argv in (["--dry-run"], ["2"], ["--menu", "x"], ["--apply"]):
                with self.subTest(argv=argv):
                    self.assertEqual(launcher.main(argv), launcher.EXIT_CONFIG)

    def test_main_without_arguments_runs_auto_with_scan_stages(self):
        captured = {}

        class FakeAuto:
            def __init__(self, checks, runs_dir):
                captured["checks"] = checks

            def run(self):
                captured["stages"] = launcher.os.environ.get("AI_AGENT_STAGES")
                return 0

        with unittest.mock.patch.dict(launcher.os.environ, {}), \
                unittest.mock.patch.object(launcher, "AutoRun", FakeAuto), \
                unittest.mock.patch.object(launcher, "Launcher", side_effect=AssertionError("menu shown")):
            self.assertEqual(launcher.main([]), 0)
        self.assertEqual(captured["stages"], "scrape_jobs.py,score_jobs.py")
        stages_check = next(c for c in captured["checks"] if c.name == "Option 3 stages (AI_AGENT_STAGES)")
        self.assertEqual(stages_check.detail, "scrape_jobs.py, score_jobs.py")  # option 2's fixed stages


class TestAutoApplySelection(TempDirCase):
    """Only an explicit, valid launcher_workflow.json selects auto-apply."""

    def write(self, text):
        path = self.tmp / launcher.WORKFLOW_FILE_NAME
        path.write_text(text, encoding="utf-8")
        return path

    def test_absent_file_keeps_scan_and_score(self):
        self.assertEqual(launcher.read_launcher_workflow(self.tmp / launcher.WORKFLOW_FILE_NAME), "scan-and-score")

    def test_only_exact_values_are_accepted(self):
        self.assertEqual(launcher.read_launcher_workflow(self.write('{"workflow": "auto-apply"}')), "auto-apply")
        self.assertEqual(launcher.read_launcher_workflow(self.write('{"workflow": "scan-and-score"}')), "scan-and-score")
        for text in ("", "{", "[]", '"auto-apply"', '{"workflow": "AUTO-APPLY"}', '{"workflow": "auto"}',
                     '{"workflow": "auto-apply", "max": 5}', '{"mode": "auto-apply"}', '{"workflow": true}'):
            with self.subTest(text=text), self.assertRaises(launcher.WorkflowBlocked):
                launcher.read_launcher_workflow(self.write(text))

    def test_a_folder_named_like_the_file_is_refused(self):
        (self.tmp / launcher.WORKFLOW_FILE_NAME).mkdir()
        with self.assertRaises(launcher.WorkflowBlocked):
            launcher.read_launcher_workflow(self.tmp / launcher.WORKFLOW_FILE_NAME)

    def run_main(self, file_text=None, env=None):
        if file_text is not None:
            self.write(file_text)
        captured = {}

        class FakeAuto:
            def __init__(self, checks, runs_dir, workflow=None, source=""):
                captured.update(checks=checks, workflow=workflow, source=source,
                                mode=launcher.os.environ.get("APPLICATION_MODE"),
                                stages=launcher.os.environ.get("AI_AGENT_STAGES"))

            def run(self):
                return 0

        with unittest.mock.patch.dict(launcher.os.environ, env or {}), \
                unittest.mock.patch.object(launcher, "PROJECT_DIR", self.tmp), \
                unittest.mock.patch.object(launcher, "AutoRun", FakeAuto), \
                unittest.mock.patch.object(launcher, "Launcher", side_effect=AssertionError("menu shown")):
            code = launcher.main([])
        return code, captured

    def test_environment_cannot_select_auto_apply(self):
        hostile = dict(HOSTILE_ENV, LAUNCHER_WORKFLOW="auto-apply", WORKFLOW="auto-apply")
        code, captured = self.run_main(env=hostile)
        self.assertEqual(code, 0)
        self.assertIsNone(captured["workflow"])  # scan and score
        self.assertEqual((captured["mode"], captured["stages"]), ("SAFE", "scrape_jobs.py,score_jobs.py"))

    def test_valid_opt_in_selects_auto_apply(self):
        code, captured = self.run_main('{"workflow": "auto-apply"}')
        workflow = captured["workflow"]
        self.assertEqual((workflow.workflow, captured["mode"]), ("auto-apply", "AUTO"))
        self.assertEqual(captured["stages"], "scrape_jobs.py,score_jobs.py,tailor_job.py")
        self.assertIn(launcher.WORKFLOW_FILE_NAME, captured["source"])
        names = [c.name for c in captured["checks"]]
        self.assertIn("Auto-apply settings", names)
        self.assertIn("Agent .env defines google_drive_folder_id", names)

    def test_invalid_opt_in_runs_nothing(self):
        code, captured = self.run_main('{"workflow": "auto-apply", "extra": 1}')
        self.assertEqual((code, captured), (launcher.EXIT_CONFIG, {}))

    def test_auto_apply_workflow_definition(self):
        workflow = launcher.build_auto_apply_workflow(7.5, 1)
        self.assertEqual(workflow.args, ("--workflow", "auto-apply", "--wait-for-login"))
        self.assertNotIn("--no-apply", workflow.args)
        env = launcher.child_environment(workflow.env, base=dict(HOSTILE_ENV, APPLICATION_MAX_JOBS="1"))
        self.assertEqual((env["APPLICATION_MODE"], env["artifact_sync"], env["GOOGLE_SHEETS_ENABLED"],
                          env["AI_AGENT_EXPORT_LIMIT"], env["APPLICATION_JOB_WINDOW_HOURS"], env["JOB_SOURCES"]),
                         ("AUTO", "0", "false", "2", "24", "naukri"))
        self.assertEqual(env["AI_AGENT_STAGES"], "scrape_jobs.py,score_jobs.py,tailor_job.py")
        self.assertEqual(env["APPLICATION_SCORE_THRESHOLD"], "8")  # 7.5 is raised to the minimum 8
        self.assertEqual(env["APPLICATION_MAX_JOBS"], "1")  # never set by the launcher: .env value kept
        self.assertEqual(launcher.build_auto_apply_workflow(9, 1).env["APPLICATION_SCORE_THRESHOLD"], "9")
        self.assertEqual(launcher.auto_apply_problems(workflow, 1), [])
        self.assertEqual(launcher.auto_apply_problems(workflow, 2), [])
        for max_jobs in (0, 3, 25):
            self.assertTrue(launcher.auto_apply_problems(workflow, max_jobs))

    def test_agent_env_key_check_reads_names_only(self):
        agent = self.tmp / "agent"
        agent.mkdir()
        [missing] = launcher.agent_env_key_checks(agent)
        self.assertEqual((missing.ok, missing.blocks), (False, (launcher.AUTO_APPLY_CHOICE,)))
        (agent / ".env").write_text("google_drive_folder_id=secret-folder\n", encoding="utf-8")
        [present] = launcher.agent_env_key_checks(agent)
        self.assertTrue(present.ok)
        self.assertNotIn("secret-folder", present.detail)

    def auto_run(self, checks=OK_CHECKS, config=(0, "CONFIGURATION ok\n"), launch=None, max_jobs=1):
        self.output, self.launch = [], launch or FakeLaunch()
        workflow = launcher.build_auto_apply_workflow(8, max_jobs)
        code = launcher.AutoRun(list(checks), self.runs, out=self.output.append, launch=self.launch,
                                check_config=lambda env: config, workflow=workflow, source="test").run()
        return code, "\n".join(self.output)

    def test_auto_apply_run_starts_the_pipeline_without_no_apply(self):
        code, text = self.auto_run()
        [(args, env)] = self.launch.pipeline_runs()
        self.assertEqual(args[:3], ["--workflow", "auto-apply", "--wait-for-login"])
        self.assertNotIn("--no-apply", args)
        self.assertEqual((env["APPLICATION_MODE"], env["GOOGLE_SHEETS_ENABLED"], env["artifact_sync"]),
                         ("AUTO", "false", "0"))
        self.assertIn("AUTO-APPLY is enabled", text)

    def test_auto_apply_blocked_by_a_failed_check_runs_nothing(self):
        blocked = [launcher.Check("Auto-apply settings", False, "APPLICATION_MAX_JOBS ... (is 5)",
                                  (launcher.AUTO_APPLY_CHOICE,))]
        code, _ = self.auto_run(checks=blocked)
        self.assertEqual((code, self.launch.calls), (1, []))
        [run_dir] = [p for p in self.runs.iterdir() if p.is_dir()]
        report = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual((report["workflow"], report["application_mode"], report["no_apply"], report["status"]),
                         ("auto-apply", "AUTO", False, "failed"))
        self.assertIsNone(report["applications"])

    def test_auto_readiness_failure_in_the_config_check_runs_nothing(self):
        code, _ = self.auto_run(config=(1, "  [FAIL] AUTO mode readiness  AUTO is refused until ...\n"))
        self.assertEqual((code, self.launch.calls), (1, []))

    def test_auto_apply_outcomes_are_shown(self):
        class AppliedLaunch(FakeLaunch):
            def __call__(self, args, env):
                code = super().__call__(args, env)
                run_dir = Path(args[args.index("--run-dir") + 1])
                report = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
                report.update(application_outcomes={"APPLIED_CONFIRMED": 1}, applications=[
                    {"job_id": 7, "outcome": "APPLIED_CONFIRMED", "title": "Java Developer", "company": "Acme",
                     "reason": "Naukri confirmed the application"}])
                (run_dir / "result.json").write_text(json.dumps(report), encoding="utf-8")
                return code

        _, text = self.auto_run(launch=AppliedLaunch())
        self.assertIn("[7] APPLIED_CONFIRMED: Java Developer @ Acme", text)


class TestLock(TempDirCase):
    def test_one_holder_at_a_time_and_released_on_close(self):
        path = self.runs / launcher.LOCK_NAME
        first, second = launcher.LauncherLock(path), launcher.LauncherLock(path)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second._handle.close()  # a crashed holder: the OS drops the lock with the handle
        second._handle = None
        third = launcher.LauncherLock(path)
        self.assertTrue(third.acquire())
        third.release()


if __name__ == "__main__":
    unittest.main()
