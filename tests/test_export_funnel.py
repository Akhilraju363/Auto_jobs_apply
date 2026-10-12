"""Export funnel to the AI Agent: per-job decisions and reasons, the per-run limit, what the Agent newly
processed this run, the summary counts and the Sheets "nothing to sync" status.

Fakes only: no browser, no Agent subprocess, no Google call. Nothing is applied (--no-apply)."""

import json

import application_pipeline as pipeline
import db
import sheets_tracker as st
from ai_agent_bridge import AgentRunResult
from experience_check import INELIGIBLE
from tests.test_application_pipeline import PipelineTestCase

LINKEDIN = "https://www.linkedin.com/jobs/view/4300000001"


class FunnelTestCase(PipelineTestCase):
    def fresh(self, job_id, **extra):
        extra.setdefault("posted_label", "2 hours ago")
        return self.add_job(job_id, scored=False, **extra)

    def run_funnel(self, settings=None, agent=None, **flags):
        self.lines = []
        flags.setdefault("no_apply", True)
        p = pipeline.ApplicationPipeline(
            settings or self.settings(), skip_scan=True, scanner_factory=lambda: None,
            browser_factory=lambda: self.browser, runner_factory=self.make_runner,
            agent_runner=agent or self.fake_agent,
            tracker_factory=lambda sheet_id, tab: st.SheetTracker(self.gws, sheet_id, tab),
            prompt=lambda message: "", out=self.lines.append, **flags)
        return p.run()

    def line_for(self, job):
        return next(line for line in self.lines if line.strip().startswith(f"[{job['id']}]"))


class TestExportDecisions(FunnelTestCase):
    def test_limit_defers_valid_jobs_with_an_explicit_reason(self):
        jobs = [self.fresh(f"90000000010{i}") for i in range(5)]
        summary = self.run_funnel(self.settings(export_limit=2))
        self.assertEqual((summary.export_candidates, summary.exported, summary.export_deferred,
                          summary.export_rejected), (5, 2, 3, 0))
        self.assertEqual([e["source_job_id"] for e in self.agent_calls[0]], ["900000000100", "900000000101"])
        for job in jobs[:2]:
            self.assertIn("EXPORTED", self.line_for(job))
        for job in jobs[2:]:
            line = self.line_for(job)
            self.assertIn("DEFERRED", line)
            self.assertIn("AI_AGENT_EXPORT_LIMIT=2", line)
            self.assertEqual(db.get_job(job["id"])["status"], "discovered")  # still offered next run
        self.assertTrue(any("5 fresh, experience-eligible, unscored job(s) -> 2 exported, 3 deferred" in line
                            for line in self.lines))

    def test_every_other_unscored_job_has_an_exclusion_reason(self):
        ok = self.fresh("900000000201")
        short = self.fresh("900000000202", job_description="Java role.", skills=[])
        stale = self.add_job("900000000203", scored=False, posted_label="3 days ago")
        unknown = self.add_job("900000000204", scored=False, posted_label="1 day ago")
        senior = self.fresh("900000000205", experience_min=5, experience_max=8, experience_text="5 - 8 years")
        conflict = self.fresh("900000000206", job_description="Minimum of 7+ years of relevant experience. " * 3)
        applied = self.fresh("900000000207")
        db.record_application_attempt(applied, "applied")
        summary = self.run_funnel()
        self.assertEqual(summary.discovered_unscored, 7)
        self.assertEqual(summary.not_fresh_unscored, 2)  # stale + unknown: never exported
        self.assertEqual((summary.experience_ineligible, summary.experience_review), (1, 1))
        self.assertEqual(summary.already_applied, 1)
        self.assertEqual((summary.export_candidates, summary.exported, summary.export_rejected), (2, 1, 1))
        self.assertIn("EXPORTED", self.line_for(ok))
        self.assertIn("REJECTED", self.line_for(short))
        self.assertIn("shorter than 80 characters", self.line_for(short))
        self.assertIn("Skipped: JD requires 5–8 years; resume supports 4+ years.", self.line_for(senior))
        self.assertIn("conflicting", self.line_for(conflict))
        self.assertEqual(db.get_job(senior["id"])["experience_check_status"], INELIGIBLE)
        exported = [e["source_job_id"] for e in self.agent_calls[0]]
        self.assertEqual(exported, ["900000000201"])
        for job in (stale, unknown, senior, conflict, applied, short):
            self.assertNotIn(job["job_id"], exported)

    def test_dry_run_counts_without_exporting(self):
        for i in range(3):
            self.fresh(f"90000000030{i}")
        summary = self.run_funnel(self.settings(export_limit=2), dry_run=True)
        self.assertEqual((summary.export_candidates, summary.exported, summary.export_deferred), (3, 2, 1))
        self.assertEqual(self.agent_calls, [])


class TestAgentActivity(FunnelTestCase):
    def agent_writing(self, extra_scored=(), extra_tailored=()):
        def agent(agent_dir, stages, export_path, python, timeout, scope_path=None):
            exported = json.loads(export_path.read_text(encoding="utf-8"))
            self.agent_calls.append(exported)
            scored_path, tailored_path = self.agent_out / "scored_jobs.json", self.agent_out / "tailored_jobs.json"
            scored = json.loads(scored_path.read_text(encoding="utf-8")) if scored_path.exists() else []
            scored += [dict(e, score=9, qualified=True) for e in exported]
            scored += [{"link": link, "score": 9, "qualified": True, "source": "LinkedIn"} for link in extra_scored]
            scored_path.write_text(json.dumps(scored), encoding="utf-8")
            tailored = [{"link": e["link"] + "?trk=1", "status": "saved"} for e in exported]
            tailored += [{"link": link, "status": "saved"} for link in extra_tailored]
            tailored += [{"link": "https://example.com/flagged", "status": "flagged_error"}]
            tailored_path.write_text(json.dumps(tailored), encoding="utf-8")
            return AgentRunResult(True, list(stages))
        return agent

    def test_counts_only_this_runs_new_work_and_flags_other_jobs(self):
        (self.agent_out / "scored_jobs.json").write_text(json.dumps(
            [{"link": "https://www.naukri.com/old-history-1", "score": 9, "qualified": True}] * 1), encoding="utf-8")
        self.fresh("900000000401")
        summary = self.run_funnel(agent=self.agent_writing(extra_scored=[LINKEDIN], extra_tailored=[LINKEDIN]))
        self.assertEqual((summary.agent_new_scored, summary.agent_new_scored_other), (1, 1))
        self.assertEqual((summary.agent_new_tailored, summary.agent_new_tailored_other), (1, 1))
        self.assertTrue(any("did not export" in w for w in summary.warnings))
        self.assertEqual((summary.scores_imported, summary.recommended), (1, 1))  # only our job is new to SQLite
        text = pipeline.format_summary(summary)
        self.assertRegex(text, r"Other jobs the Agent processed:\s+1 scored, 1 tailored")

    def test_no_warning_when_the_agent_only_processed_our_export(self):
        self.fresh("900000000402")
        summary = self.run_funnel(agent=self.agent_writing())
        self.assertEqual((summary.agent_new_scored, summary.agent_new_scored_other,
                          summary.agent_new_tailored, summary.agent_new_tailored_other), (1, 0, 1, 0))
        self.assertFalse(any("did not export" in w for w in summary.warnings))

    def test_historical_scores_are_not_reported_as_new(self):
        job = self.add_job("900000000403", score=90.0, match_status="recommended")  # scored in an earlier run
        (self.agent_out / "scored_jobs.json").write_text(json.dumps(
            [{"link": job["url"], "score": 9, "reasoning": "x", "matched_must_haves": [], "missing_must_haves": []}]),
            encoding="utf-8")
        summary = self.run_funnel()
        self.assertEqual((summary.scores_imported, summary.recommended, summary.exported), (0, 0, 0))
        self.assertEqual(summary.agent_status, "SKIPPED (no fresh unscored jobs)")
        self.assertIsNone(summary.agent_new_scored)  # the Agent did not run: unknown, not "0 new"

    def test_unreadable_agent_output_is_unknown_not_zero(self):
        self.fresh("900000000404")

        def agent(agent_dir, stages, export_path, python, timeout, scope_path=None):
            (self.agent_out / "scored_jobs.json").write_text("{not json", encoding="utf-8")
            return AgentRunResult(True, list(stages))
        summary = self.run_funnel(agent=agent)
        self.assertIsNone(summary.agent_new_scored)


class TestSummaryAndSheets(FunnelTestCase):
    def test_summary_separates_the_stages(self):
        for i in range(3):
            self.fresh(f"90000000050{i}")
        text = pipeline.format_summary(self.run_funnel(self.settings(export_limit=1)))
        for label in ("EXPORT TO AI AGENT (this run)", "Fresh, experience-eligible:", "exported to AI Agent:",
                      "deferred (limit 1/run):", "AI AGENT (this run's exported jobs only)",
                      "AI SCORING (imported into SQLite)", "This run's exports scored:",
                      "ELIGIBILITY (application candidates)", "Recommended, not applied (all runs):",
                      "Eligible to apply now:"):
            self.assertIn(label, text)
        self.assertRegex(text, r"deferred \(limit 1/run\):\s+2")

    def test_nothing_pending_is_up_to_date_not_synced(self):
        summary = self.run_funnel()
        self.assertEqual((summary.sheets.status, summary.sheets.synced, summary.sheets.pending),
                         (st.UP_TO_DATE, 0, 0))
        self.assertEqual(self.gws.rows, [])  # nothing was written
        self.assertEqual(summary.exit_code, pipeline.EXIT_OK)

    def test_pending_outcome_still_reports_synced(self):
        job = self.fresh("900000000601")
        db.set_application_state(job["id"], "external_application", "EXTERNAL_APPLICATION", "company site")
        db.queue_sheet_sync(job["id"])
        report = st.sync_pending_outcomes(st.SheetTracker(self.gws, "sheet-id", "Sheet1"))
        self.assertEqual((report.status, report.synced, report.pending), (st.SYNCED, 1, 0))
