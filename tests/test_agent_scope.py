"""Scoped AI Agent runs from the Naukri side: the scope file holds exactly this run's exported jobs,
only the Agent's scoring/tailoring stages get --job-ids-file, an Agent without scope support is never
run unscoped, and the summary counts this run's results apart from historical Agent data.

Fakes only: the Agent subprocess, browser and Sheets are faked; temporary databases; nothing applied."""

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ai_agent_bridge as bridge
import application_pipeline as pipeline
import db
import sheets_tracker as st
from ai_agent_bridge import AgentRunResult
from tests.test_application_pipeline import PipelineTestCase


class FakeAgentDir:
    def __init__(self, scoped=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / "scripts").mkdir()
        for name in ("scrape_jobs.py", "score_jobs.py", "tailor_job.py", "company_research.py"):
            (self.path / "scripts" / name).write_text("", encoding="utf-8")
        if scoped:
            (self.path / "scripts" / "job_scope.py").write_text("", encoding="utf-8")
        self.python = self.path / "python.exe"
        self.python.write_text("", encoding="utf-8")


class TestRunAgentStages(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.scope = Path(tempfile.mkdtemp()) / bridge.SCOPE_FILE_NAME
        self.scope.write_text(json.dumps({"run_id": "r1", "jobs": []}), encoding="utf-8")

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def run_stages(self, agent, stages=("scrape_jobs.py", "score_jobs.py", "tailor_job.py"), scope=True):
        return bridge.run_agent_stages(agent.path, list(stages), self.scope.with_name("naukri_jobs.json"),
                                       agent.python, runner=self.runner, scope_path=self.scope if scope else None)

    def test_only_scoring_and_tailoring_get_the_scope(self):
        result = self.run_stages(FakeAgentDir())
        self.assertTrue(result.ok)
        flags = {Path(argv[1]).name: argv[2:] for argv in self.calls}
        self.assertEqual(flags["scrape_jobs.py"], [])
        for stage in ("score_jobs.py", "tailor_job.py"):
            self.assertEqual(flags[stage], ["--job-ids-file", str(self.scope.resolve())])

    def test_agent_without_scope_support_is_not_run(self):
        result = self.run_stages(FakeAgentDir(scoped=False))
        self.assertEqual((result.ok, result.code), (False, bridge.AGENT_SCOPE_UNSUPPORTED))
        self.assertEqual(self.calls, [])  # never an unscoped fallback

    def test_stage_that_cannot_be_scoped_is_refused(self):
        result = self.run_stages(FakeAgentDir(), stages=("score_jobs.py", "company_research.py"))
        self.assertEqual((result.ok, result.code), (False, bridge.AGENT_SCOPE_UNSUPPORTED))
        self.assertIn("company_research.py", result.message)
        self.assertEqual(self.calls, [])

    def test_missing_scope_file_is_refused(self):
        self.scope.unlink()
        self.assertEqual(self.run_stages(FakeAgentDir()).code, bridge.AGENT_SCOPE_UNSUPPORTED)
        self.assertEqual(self.calls, [])

    def test_unscoped_call_is_unchanged(self):
        self.assertTrue(self.run_stages(FakeAgentDir(scoped=False), scope=False).ok)
        self.assertTrue(all(len(argv) == 2 for argv in self.calls))

    def test_report_is_read_only_for_this_run(self):
        out = Path(tempfile.mkdtemp())
        self.assertIsNone(bridge.read_scope_report(out, "r1"))  # missing: unknown
        (out / bridge.SCOPE_REPORT_NAME).write_text(json.dumps({"run_id": "r0", "score": {}}), encoding="utf-8")
        self.assertIsNone(bridge.read_scope_report(out, "r1"))  # another run's report
        (out / bridge.SCOPE_REPORT_NAME).write_text("{oops", encoding="utf-8")
        self.assertIsNone(bridge.read_scope_report(out, "r1"))
        (out / bridge.SCOPE_REPORT_NAME).write_text(json.dumps({"run_id": "r1", "score": {}}), encoding="utf-8")
        self.assertEqual(bridge.read_scope_report(out, "r1")["run_id"], "r1")


class ScopedPipelineCase(PipelineTestCase):
    def fresh(self, job_id, **extra):
        extra.setdefault("posted_label", "2 hours ago")
        return self.add_job(job_id, scored=False, **extra)

    def run_scoped(self, limit=2, agent=None, clock=None, **flags):
        flags.setdefault("no_apply", True)
        self.lines = []
        options = dict(clock=clock) if clock else {}
        p = pipeline.ApplicationPipeline(
            self.settings(export_limit=limit), skip_scan=True, scanner_factory=lambda: None,
            browser_factory=lambda: self.browser, runner_factory=self.make_runner,
            agent_runner=agent or self.scoring_agent,
            tracker_factory=lambda sheet_id, tab: st.SheetTracker(self.gws, sheet_id, tab),
            prompt=lambda message: "", out=self.lines.append, **flags, **options)
        return p.run()

    def scope_ids(self, index=-1):
        return [j["id"] for j in json.loads(Path(self.agent_scopes[index]).read_text(encoding="utf-8"))["jobs"]]

    def scoring_agent(self, agent_dir, stages, export_path, python, timeout, scope_path=None, score=9):
        """Like the scoped Agent: scores exactly the scope, keeps history, writes the run report."""
        self.agent_scopes.append(scope_path)
        scope = json.loads(Path(scope_path).read_text(encoding="utf-8"))
        exported = {e["link"]: e for e in json.loads(Path(export_path).read_text(encoding="utf-8"))}
        path = self.agent_out / "scored_jobs.json"
        history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        done = {e["link"] for e in history}
        new = [dict(exported[j["link"]], score=score, reasoning="fit", matched_must_haves=[], missing_must_haves=[],
                    qualified=score >= 8) for j in scope["jobs"] if j["link"] not in done]
        path.write_text(json.dumps(history + new), encoding="utf-8")
        ids = {e["link"]: e["source_job_id"] for e in exported.values()}
        self.write_agent_report(scope_path,
                                score={"newly_scored": [ids[e["link"]] for e in new],
                                       "already_scored": [j["id"] for j in scope["jobs"] if j["link"] in done],
                                       "missing": [], "failed": []},
                                tailor={"newly_tailored": [ids[e["link"]] for e in new if e["qualified"]],
                                        "already_tailored": [], "not_qualified": [], "flagged": []})
        return AgentRunResult(True, list(stages))


class TestScopedPipeline(ScopedPipelineCase):
    def test_scope_is_exactly_the_exported_jobs_at_the_default_cap(self):
        jobs = [self.fresh(f"90000000070{i}") for i in range(5)]
        summary = self.run_scoped(limit=2)
        self.assertEqual(self.scope_ids(), ["900000000700", "900000000701"])
        self.assertEqual(summary.exported_ids, [jobs[0]["id"], jobs[1]["id"]])
        self.assertEqual((summary.exported, summary.export_deferred), (2, 3))
        report = summary.scope_report
        self.assertEqual((report["score"]["newly_scored"], report["tailor"]["newly_tailored"]),
                         (["900000000700", "900000000701"], ["900000000700", "900000000701"]))
        self.assertEqual((summary.exported_scored, summary.exported_recommended), (2, 2))
        for job in jobs[2:]:
            self.assertEqual(db.get_job(job["id"])["status"], "discovered")  # deferred, not scored

    def test_a_configured_higher_cap_is_respected(self):
        for i in range(5):
            self.fresh(f"90000000071{i}")
        summary = self.run_scoped(limit=5)
        self.assertEqual(len(self.scope_ids()), 5)
        self.assertEqual((summary.exported, summary.export_deferred), (5, 0))

    def test_deferred_jobs_are_offered_next_run_while_fresh(self):
        now = datetime.now(timezone.utc)
        early = [self.fresh(f"90000000072{i}") for i in range(2)]
        later = self.fresh("900000000722", posted_label="2 hours ago")
        expiring = self.fresh("900000000723", posted_label="20 hours ago")
        self.run_scoped(limit=2, clock=lambda: now)
        self.assertEqual(self.scope_ids(), [early[0]["job_id"], early[1]["job_id"]])
        summary = self.run_scoped(limit=2, clock=lambda: now + timedelta(hours=6))  # "20 hours ago" is now stale
        self.assertEqual(self.scope_ids(), [later["job_id"]])
        self.assertEqual(summary.not_fresh_unscored, 1)
        self.assertEqual(db.get_job(expiring["id"])["status"], "discovered")  # kept, just no longer exported

    def test_repeated_runs_count_only_their_own_work(self):
        self.fresh("900000000730")
        first = self.run_scoped()
        second = self.run_scoped()
        self.assertEqual(len(first.scope_report["score"]["newly_scored"]), 1)
        self.assertEqual((second.exported, second.scope_report, second.exported_scored), (0, None, 0))
        self.assertEqual(second.agent_status, "SKIPPED (no fresh unscored jobs)")
        self.assertEqual(len(self.agent_scopes), 1)  # nothing exported, so the Agent did not run at all

    def test_historical_recommendations_are_not_this_runs(self):
        self.add_job("900000000740", score=90.0, match_status="recommended")  # recommended in an earlier run
        self.fresh("900000000741")
        summary = self.run_scoped(agent=lambda *a, **k: self.scoring_agent(*a, **k, score=5))
        self.assertEqual((summary.exported_scored, summary.exported_recommended), (1, 0))
        self.assertEqual(summary.candidates, 1)  # the historical one, shown as such
        text = pipeline.format_summary(summary)
        self.assertRegex(text, r"This run's exports scored:\s+1")
        self.assertRegex(text, r"recommended \(8\+\):\s+0")
        self.assertRegex(text, r"Recommended, not applied \(all runs\):\s+1")

    def test_agent_failure_is_reported_without_an_unscoped_retry(self):
        self.fresh("900000000750")
        calls = []

        def failing(*args, **kwargs):
            calls.append(kwargs.get("scope_path"))
            return AgentRunResult(False, failed_stage="score_jobs.py", code=bridge.AGENT_SCOPE_UNSUPPORTED,
                                  message="no scoped runs")
        summary = self.run_scoped(agent=failing)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0])  # the only call was scoped
        self.assertIn(bridge.AGENT_SCOPE_UNSUPPORTED, summary.agent_status)
        self.assertIsNone(summary.scope_report)
        self.assertRegex(pipeline.format_summary(summary), r"Newly scored:\s+-\n")  # not run: no counts

    def test_missing_report_is_unknown_and_flagged(self):
        self.fresh("900000000760")

        def silent(agent_dir, stages, export_path, python, timeout, scope_path=None):
            return AgentRunResult(True, list(stages))
        summary = self.run_scoped(agent=silent)
        self.assertIsNone(summary.scope_report)
        self.assertTrue(any("wrote no report for this scoped run" in w for w in summary.warnings))
        self.assertIn("unknown (no report for this run)", pipeline.format_summary(summary))

    def test_a_stage_that_did_not_report_is_unknown_not_zero(self):
        self.fresh("900000000765")

        def tailoring_stopped(agent_dir, stages, export_path, python, timeout, scope_path=None):
            self.write_agent_report(scope_path, score={"newly_scored": ["900000000765"], "already_scored": [],
                                                       "missing": [], "failed": []})  # no "tailor" section
            return AgentRunResult(False, ["scrape_jobs.py", "score_jobs.py"], "tailor_job.py",
                                  "AGENT_STAGE_FAILED", "tailor_job.py exited with code 1")
        text = pipeline.format_summary(self.run_scoped(agent=tailoring_stopped))
        self.assertRegex(text, r"Newly scored:\s+1\n")
        self.assertRegex(text, r"Newly tailored:\s+unknown")
        self.assertRegex(text, r"Already processed \(skipped\):\s+0 scored, unknown tailored")

    def test_summary_counts_reconcile_with_job_ids(self):
        jobs = [self.fresh(f"90000000077{i}") for i in range(3)]
        short = self.fresh("900000000779", job_description="Java.", skills=[])
        stale = self.add_job("900000000778", scored=False, posted_label="3 days ago")
        summary = self.run_scoped(limit=2)
        decided = {decision for line in self.lines for decision in ("EXPORTED", "DEFERRED", "REJECTED") if decision in line}
        self.assertEqual(decided, {"EXPORTED", "DEFERRED", "REJECTED"})
        self.assertEqual(summary.discovered_unscored, 5)
        self.assertEqual(summary.not_fresh_unscored + summary.export_candidates, 5)
        self.assertEqual(summary.export_candidates, summary.exported + summary.export_deferred + summary.export_rejected)
        self.assertEqual(len(summary.exported_ids), summary.exported)
        self.assertEqual(sorted(summary.exported_ids), [jobs[0]["id"], jobs[1]["id"]])
        self.assertEqual(sorted(self.scope_ids()), sorted(j["job_id"] for j in jobs[:2]))
        self.assertNotIn(short["job_id"], self.scope_ids())
        self.assertNotIn(stale["job_id"], self.scope_ids())

    def test_safe_mode_never_applies_without_approval(self):
        self.add_job("900000000001", 90.0, "recommended", posted_label="2 hours ago")
        summary = self.run_scoped(no_apply=False)  # the prompt answers "" (Enter) to everything
        self.assertEqual((summary.applied, self.browser.clicks, self.browser.submits), (0, 0, 0))
        self.assertEqual(summary.mode, "SAFE")


class TestExportLimitConfig(unittest.TestCase):
    def test_effective_cap_is_visible_and_invalid_values_fall_back(self):
        unset = pipeline.export_limit_check("", 2)
        self.assertTrue(unset.ok)
        self.assertIn("default 2", unset.detail)
        self.assertTrue(pipeline.export_limit_check("10", 10).ok)
        for raw, effective in (("abc", 2), ("2.5", 2), ("0", 1), ("-3", 1)):
            with self.subTest(raw):
                check = pipeline.export_limit_check(raw, effective)
                self.assertFalse(check.ok)
                self.assertFalse(check.fatal)
                self.assertIn(f"using {effective}", check.detail)

    def test_default_cap_is_two(self):
        import config
        if not config.AI_AGENT_EXPORT_LIMIT_SETTING:
            self.assertEqual(config.AI_AGENT_EXPORT_LIMIT, 2)
        self.assertEqual(pipeline.PipelineSettings().export_limit, config.AI_AGENT_EXPORT_LIMIT)


if __name__ == "__main__":
    unittest.main()
