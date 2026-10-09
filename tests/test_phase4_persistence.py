"""Phase 4: SQLite location/migration, application history identity, the Google Sheets outbox and
tracker, the AI Agent runner and freshness anchored to scrape time.

No network, no Google, no AI Agent: gws and subprocesses are fakes.
"""

import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import ai_agent_bridge as bridge
import db
import sheets_tracker as st
from eligibility import FRESH, STALE, UNKNOWN, evaluate_job, job_freshness
from naukri_parser import parse_card

INFOSYS_A = "https://www.naukri.com/job-listings-java-full-stack-developer-infosys-bengaluru-3-to-5-years-111111111111"
INFOSYS_B = "https://www.naukri.com/job-listings-java-developer-infosys-bengaluru-3-to-5-years-222222222222"


class TempDb(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # sqlite handles linger on Windows
        self.tmp = Path(self._tmp.name)
        patcher = mock.patch.object(db, "DB_PATH", self.tmp / "data" / "naukri_auto_apply.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        db.init_db()

    def add(self, job_id, url, title="Java Full Stack Developer", company="Infosys", **extra):
        job = parse_card({"job_id": job_id, "url": url, "title": title, "company": company,
                          "location": "Bengaluru"}).to_dict()
        job.update(extra)
        db.save_discovered_job(job)
        return next(j for j in db.get_jobs(status=None) if j["url"] == db.canonical_job_url(url))

    def apply(self, job):
        db.set_application_state(job["id"], "applied", "APPLIED", "Naukri confirmed the application")
        db.record_application_attempt(job, "applied", "C:/resumes/v1.pdf")
        return db.get_job(job["id"])


class TestSqliteInitialization(TempDb):
    def test_tables_created_in_data_folder_and_idempotent(self):
        self.assertTrue(db.DB_PATH.exists())
        db.init_db()  # second run migrates nothing and loses nothing
        with sqlite3.connect(db.DB_PATH) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"jobs", "applied_jobs", "application_attempts", "sheet_sync"} <= tables)

    def test_job_persists_between_executions(self):
        job = self.add("111111111111", INFOSYS_A)
        db.init_db()
        self.assertEqual(db.get_job(job["id"])["title"], "Java Full Stack Developer")

    def test_legacy_jobs_db_copied_once_and_left_untouched(self):
        legacy, target = self.tmp / "jobs.db", self.tmp / "new" / "naukri_auto_apply.db"
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE marker (v TEXT)")
            conn.execute("INSERT INTO marker VALUES ('old data')")
        before = legacy.read_bytes()
        with mock.patch.object(db, "DB_PATH", target), mock.patch.object(db, "DEFAULT_DB_PATH", target), \
                mock.patch.object(db, "LEGACY_DB_PATH", legacy):
            db.init_db()
            with sqlite3.connect(target) as conn:
                self.assertEqual(conn.execute("SELECT v FROM marker").fetchone()[0], "old data")
                conn.execute("INSERT INTO marker VALUES ('new data')")
            db.init_db()  # already migrated: never copied over again
            with sqlite3.connect(target) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0], 2)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse((target.parent / "naukri_auto_apply.db.migrating").exists())

    def test_no_migration_for_a_custom_database_path(self):
        legacy, custom = self.tmp / "jobs.db", self.tmp / "custom.db"
        with sqlite3.connect(legacy) as conn:
            conn.execute("CREATE TABLE marker (v TEXT)")
        with mock.patch.object(db, "DB_PATH", custom), mock.patch.object(db, "LEGACY_DB_PATH", legacy):
            db.init_db()
            with sqlite3.connect(custom) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("marker", tables)


class TestApplicationHistory(TempDb):
    def test_canonical_url(self):
        self.assertEqual(db.canonical_job_url("HTTP://WWW.Naukri.com/job-listings-x-1/?src=a#top"),
                         "https://www.naukri.com/job-listings-x-1")
        self.assertIsNone(db.canonical_job_url("not a url"))
        self.assertIsNone(db.canonical_job_url(None))

    def test_successful_application_lookup(self):
        job = self.apply(self.add("111111111111", INFOSYS_A))
        self.assertTrue(db.has_successful_application(job))
        self.assertEqual(db.find_successful_application(job)["resume_path"], "C:/resumes/v1.pdf")
        self.assertEqual(db.count_successful_applications(), 1)

    def test_same_company_different_job_stays_eligible(self):
        self.apply(self.add("111111111111", INFOSYS_A))
        other = self.add("222222222222", INFOSYS_B, title="Java Developer")
        self.assertFalse(db.has_successful_application(other))
        self.assertEqual(other["company"], "Infosys")

    def test_matched_by_canonical_url_when_rediscovered_without_job_id(self):
        applied = self.add(None, INFOSYS_A)  # no Naukri id: applied_jobs key is jobrow:<id>
        self.apply(applied)
        rediscovered = {"id": 999, "job_id": None, "url": INFOSYS_A + "?src=jobsearch&sid=1",
                        "dedup_key": "naukri:url:other"}
        self.assertTrue(db.has_successful_application(rediscovered))

    def test_matched_by_naukri_job_id_and_by_dedup_key(self):
        job = self.apply(self.add("111111111111", INFOSYS_A))
        self.assertTrue(db.has_successful_application({"id": 999, "job_id": "111111111111",
                                                       "url": "https://www.naukri.com/moved"}))
        self.assertTrue(db.has_successful_application({"id": 999, "url": None, "dedup_key": job["dedup_key"]}))

    def test_unconfirmed_attempt_is_not_success(self):
        job = self.add("111111111111", INFOSYS_A)
        db.record_application_attempt(job, "unconfirmed")
        self.assertFalse(db.has_successful_application(job))

    def test_applied_record_is_immutable(self):
        job = self.apply(self.add("111111111111", INFOSYS_A))
        self.assertFalse(db.record_application_attempt(job, "submission_failed", failure_reason="later"))
        self.assertEqual(db.get_application_record(job)["status"], "applied")


class TestSheetSyncOutbox(TempDb):
    def test_queue_sync_and_requeue_on_new_outcome(self):
        job = self.apply(self.add("111111111111", INFOSYS_A))
        self.assertTrue(db.queue_sheet_sync(job["id"]))
        self.assertEqual([r["status"] for r in db.get_sheet_syncs()], ["PENDING"])
        self.assertTrue(db.mark_sheet_synced(job["id"], "applied", "APPLIED"))
        self.assertEqual(db.get_sheet_syncs(), [])
        self.assertFalse(db.queue_sheet_sync(job["id"]))  # same outcome already synced
        db.set_application_state(job["id"], "already_applied", "ALREADY_APPLIED")
        self.assertTrue(db.queue_sheet_sync(job["id"]))
        self.assertFalse(db.queue_sheet_sync(12345))

    def test_mark_synced_ignores_a_stale_outcome(self):
        job = self.apply(self.add("111111111111", INFOSYS_A))
        db.queue_sheet_sync(job["id"])
        self.assertFalse(db.mark_sheet_synced(job["id"], "failed", None))
        self.assertEqual(len(db.get_sheet_syncs()), 1)

    def test_failure_keeps_pending_and_never_touches_history(self):
        job = self.apply(self.add("111111111111", INFOSYS_A))
        db.queue_sheet_sync(job["id"])
        db.mark_sheet_sync_failed(job["id"], "AUTH_REQUIRED", "gws auth login needed")
        row = db.get_sheet_syncs()[0]
        self.assertEqual((row["status"], row["last_error_code"], row["attempts"]), ("PENDING", "AUTH_REQUIRED", 1))
        self.assertEqual(db.get_job(job["id"])["application_state"], "applied")
        self.assertTrue(db.has_successful_application(job))


# ---------------------------------------------------------------------------
# Google Sheets tracker (fake gws)
# ---------------------------------------------------------------------------


def _col_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + ord(ch) - 64
    return index - 1


class FakeGws:
    """In-memory sheet behind the gws argument format used by sheets_tracker."""

    def __init__(self, rows=None, fail=None):
        self.rows = [list(r) for r in (rows or [])]
        self.calls, self.fail = [], fail

    def __call__(self, *args):
        self.calls.append(args)
        if self.fail:
            raise st.SheetsError(self.fail, "simulated gws failure")
        op = args[3]
        params = json.loads(args[args.index("--params") + 1])
        if op == "get":
            return {"values": [list(r) for r in self.rows]}
        values = json.loads(args[args.index("--json") + 1])["values"]
        if op == "append":
            self.rows.extend(list(v) for v in values)
        elif op == "update":
            cells = params["range"].split("!")[1]
            start = re.match(r"([A-Z]+)(\d+)", cells.split(":")[0])
            col, row = _col_index(start.group(1)), int(start.group(2)) - 1
            for r_offset, line in enumerate(values):
                while len(self.rows) <= row + r_offset:
                    self.rows.append([])
                target = self.rows[row + r_offset]
                for c_offset, value in enumerate(line):
                    while len(target) <= col + c_offset:
                        target.append("")
                    target[col + c_offset] = value
        return {}

    def writes(self):
        return [c for c in self.calls if c[3] in ("update", "append")]


NOW = datetime(2026, 10, 8, 10, 30)


class TestSheetTracker(TempDb):
    def tracker(self, gws):
        return st.SheetTracker(gws, "sheet-id", clock=lambda: NOW)

    def scored_applied_job(self, job_id="111111111111", url=INFOSYS_A):
        job = self.add(job_id, url, experience_text="3-5 Yrs")
        db.save_job_score(job["id"], 90, "recommended", "fits")
        return self.apply(db.get_job(job["id"]))

    def test_headers_match_the_ai_agent_tracker(self):
        self.assertEqual(st.AGENT_HEADERS, ["Job Title", "Company", "Job Link", "Fit Score", "Resume Path", "Status",
                                            "Timestamp", "Company Notes", "Source", "Status Updated", "Resume ID",
                                            "Match %"])
        self.assertEqual(len(st.HEADERS), 19)

    def test_appends_full_row_on_empty_sheet(self):
        gws = FakeGws()
        job = self.scored_applied_job()
        st.SheetTracker(gws, "sheet-id", clock=lambda: NOW).upsert(job, db.find_successful_application(job))
        self.assertEqual(gws.rows[0], st.HEADERS)
        row = dict(zip(st.HEADERS, gws.rows[1]))
        self.assertEqual((row["Job Title"], row["Company"], row["Job Link"]), ("Java Full Stack Developer", "Infosys", INFOSYS_A))
        self.assertEqual((row["Fit Score"], row["Status"], row["Source"]), ("9", "Applied", "Naukri"))
        self.assertEqual((row["Application State"], row["Naukri Job ID"], row["AI Recommendation"]),
                         ("APPLIED", "111111111111", "recommended"))
        self.assertEqual((row["Location"], row["Experience"], row["Resume Path"]), ("Bengaluru", "3-5 Yrs", "C:/resumes/v1.pdf"))
        self.assertTrue(row["Applied At"])
        self.assertEqual(row["Failure Reason"], "")

    def test_updates_the_agents_existing_row_instead_of_duplicating(self):
        agent_row = ["Java Full Stack Developer", "Infosys", INFOSYS_A + "?src=x", "9", "https://drive/x", "Not Applied",
                     "2026-10-07", "notes", "Naukri", "", "rid1", "88"]
        gws = FakeGws([st.AGENT_HEADERS, agent_row])
        job = self.scored_applied_job()
        self.tracker(gws).upsert(job, db.find_successful_application(job))
        self.assertEqual(len(gws.rows), 2)
        self.assertEqual(gws.rows[0][12:], st.NAUKRI_HEADERS)
        row = dict(zip(st.HEADERS, gws.rows[1]))
        self.assertEqual((row["Status"], row["Status Updated"], row["Application State"]), ("Applied", "2026-10-08", "APPLIED"))
        self.assertEqual((row["Resume Path"], row["Company Notes"], row["Resume ID"]), ("https://drive/x", "notes", "rid1"))

    def test_user_status_is_never_overwritten(self):
        self.assertEqual(st.tracker_status("APPLIED", "Interviewing"), "Interviewing")
        self.assertEqual(st.tracker_status("FAILED", "Applied"), "Applied")
        self.assertEqual(st.tracker_status("FAILED", ""), "Not Applied")
        self.assertEqual(st.tracker_status("APPLIED", "Not Applied"), "Applied")

    def test_formula_like_text_is_quoted(self):
        self.assertEqual(st._cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(st._cell("Java"), "Java")

    def test_sync_success(self):
        job = self.scored_applied_job()
        db.queue_sheet_sync(job["id"])
        report = st.sync_pending_outcomes(self.tracker(FakeGws()))
        self.assertEqual((report.status, report.synced, report.pending), ("SYNCED", 1, 0))

    def test_sync_failure_leaves_applied_and_pending_then_retry_succeeds(self):
        jobs = [self.scored_applied_job(), self.scored_applied_job("222222222222", INFOSYS_B)]
        for job in jobs:
            db.queue_sheet_sync(job["id"])
        failing = FakeGws(fail=st.AUTH_REQUIRED)
        report = st.sync_pending_outcomes(self.tracker(failing))
        self.assertEqual((report.status, report.synced, report.pending), ("AUTH_REQUIRED", 0, 2))
        self.assertEqual(len(failing.calls), 1)  # auth failure stops the pass: one call, not one per job
        for job in jobs:
            self.assertEqual(db.get_job(job["id"])["application_state"], "applied")
            self.assertTrue(db.has_successful_application(job))
        self.assertEqual({r["last_error_code"] for r in db.get_sheet_syncs()}, {"AUTH_REQUIRED"})
        working = FakeGws()
        report = st.sync_pending_outcomes(self.tracker(working))
        self.assertEqual((report.status, report.synced, report.pending), ("SYNCED", 2, 0))
        self.assertEqual(len(working.rows), 3)

    def test_unconfigured_or_disabled_tracker_keeps_rows_pending(self):
        job = self.scored_applied_job()
        db.queue_sheet_sync(job["id"])
        report = st.sync_pending_outcomes(None, st.SHEET_NOT_CONFIGURED, "no sheet id")
        self.assertEqual((report.status, report.pending), ("SHEET_NOT_CONFIGURED", 1))
        self.assertEqual(st.sync_pending_outcomes(None, st.DISABLED).status, "DISABLED")
        self.assertEqual(len(db.get_sheet_syncs()), 1)


class TestGwsClient(unittest.TestCase):
    def run_with(self, returncode=0, stdout="", stderr="", raises=None):
        def runner(*args, **kwargs):
            if raises:
                raise raises
            return subprocess.CompletedProcess(args[0], returncode, stdout, stderr)
        return st.GwsClient(["gws"], runner=runner)

    def test_success_and_error_classification(self):
        self.assertEqual(self.run_with(stdout='{"values": [["a"]]}')("sheets"), {"values": [["a"]]})
        with self.assertRaises(st.SheetsError) as auth:
            self.run_with(1, stderr="Error: invalid_grant: Token has been expired or revoked")("sheets")
        self.assertEqual(auth.exception.code, st.AUTH_REQUIRED)
        with self.assertRaises(st.SheetsError) as other:
            self.run_with(1, stderr="503 backend error")("sheets")
        self.assertEqual(other.exception.code, st.SYNC_FAILED)
        with self.assertRaises(st.SheetsError) as missing:
            self.run_with(raises=FileNotFoundError())("sheets")
        self.assertEqual(missing.exception.code, st.GWS_NOT_INSTALLED)

    def test_sheet_id_comes_from_config_or_only_the_agents_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text("google_sheet_id=agent-sheet\nGROQ_API_KEY=secret\n", encoding="utf-8")
            self.assertEqual(st.resolve_sheet_id("", Path(tmp)), "agent-sheet")
            self.assertEqual(st.resolve_sheet_id("mine", Path(tmp)), "mine")
        self.assertIsNone(st.resolve_sheet_id("", None))


# ---------------------------------------------------------------------------
# AI Agent runner and export filter
# ---------------------------------------------------------------------------


class TestAgentRunner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.agent = Path(self._tmp.name) / "agent"
        (self.agent / "scripts").mkdir(parents=True)
        for name in ("scrape_jobs.py", "score_jobs.py"):
            (self.agent / "scripts" / name).write_text("", encoding="utf-8")
        self.python = Path(self._tmp.name) / "python.exe"
        self.python.write_text("", encoding="utf-8")

    def test_runs_stages_in_order_with_naukri_handoff(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((Path(cmd[1]).name, kwargs["cwd"], kwargs["env"]))
            return subprocess.CompletedProcess(cmd, 0)

        result = bridge.run_agent_stages(self.agent, ["scrape_jobs.py", "score_jobs.py"], Path("exports/x.json"),
                                         self.python, runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual([c[0] for c in calls], ["scrape_jobs.py", "score_jobs.py"])
        self.assertEqual(calls[0][1], str(self.agent))
        self.assertEqual(calls[0][2]["JOB_SOURCES"], "naukri")
        self.assertEqual(calls[0][2]["NAUKRI_JOBS_PATH"], str(Path("exports/x.json").resolve()))

    def test_stops_at_first_failure_and_reports_missing_pieces(self):
        runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 3)  # noqa: E731
        result = bridge.run_agent_stages(self.agent, ["scrape_jobs.py", "score_jobs.py"], None, self.python, runner=runner)
        self.assertEqual((result.ok, result.failed_stage, result.code), (False, "scrape_jobs.py", bridge.AGENT_STAGE_FAILED))
        self.assertEqual(bridge.run_agent_stages(None, ["x"]).code, bridge.AGENT_NOT_CONFIGURED)
        self.assertEqual(bridge.run_agent_stages(self.agent, ["tailor_job.py"], None, self.python).code,
                         bridge.AGENT_NOT_FOUND)

        def timeout(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        self.assertEqual(bridge.run_agent_stages(self.agent, ["score_jobs.py"], None, self.python, runner=timeout).code,
                         bridge.AGENT_TIMEOUT)

    def test_own_env_keys_never_leak_into_the_agent(self):
        own = Path(self._tmp.name) / ".env"
        own.write_text("GEMINI_API_KEY=ours\nAPPLICATION_MODE=SAFE\n", encoding="utf-8")
        env = bridge.agent_environment(Path("x.json"), {"GEMINI_API_KEY": "ours", "PATH": "p"}, own)
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertEqual(env["PATH"], "p")


class TestExportFilter(TempDb):
    def test_only_accepted_jobs_are_exported(self):
        keep = self.add("111111111111", INFOSYS_A, job_description="Spring Boot microservices and Angular " * 5)
        self.add("222222222222", INFOSYS_B, job_description="Spring Boot microservices and Angular " * 5)
        result = bridge.export_discovered_jobs(self.tmp / "export.json", 5, job_filter=lambda j: j["id"] == keep["id"])
        self.assertEqual((result.discovered, result.filtered_out, len(result.exported)), (2, 1, 1))
        self.assertEqual(json.loads((self.tmp / "export.json").read_text(encoding="utf-8"))[0]["link"], INFOSYS_A)


# ---------------------------------------------------------------------------
# 24-hour freshness anchored to scrape time
# ---------------------------------------------------------------------------


class TestFreshnessAnchoring(unittest.TestCase):
    REF = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)

    def job(self, label, scraped_hours_ago, **extra):
        scraped = (self.REF - timedelta(hours=scraped_hours_ago)).astimezone().replace(tzinfo=None)
        return dict({"posted_label": label, "scraped_at": scraped.isoformat(timespec="seconds")}, **extra)

    def test_label_is_aged_since_the_scan(self):
        self.assertEqual(job_freshness(self.job("5 hours ago", 1), reference_time=self.REF)[0], FRESH)
        self.assertEqual(job_freshness(self.job("5 hours ago", 20), reference_time=self.REF)[0], STALE)
        self.assertEqual(job_freshness(self.job("Today", 72), reference_time=self.REF)[0], STALE)
        self.assertEqual(job_freshness(self.job("1 day ago", 0), reference_time=self.REF)[0], UNKNOWN)

    def test_exact_timestamp_and_found_at_rules(self):
        posted = (self.REF - timedelta(hours=2)).isoformat()
        self.assertEqual(job_freshness(self.job("3 days ago", 100, posted_at=posted), reference_time=self.REF)[0], FRESH)
        # found_at / first_seen_at never make a job fresh
        self.assertEqual(job_freshness({"first_seen_at": self.REF.isoformat(), "found_at": "2026-10-08"},
                                       reference_time=self.REF)[0], UNKNOWN)

    def test_stale_label_blocks_eligibility(self):
        base = dict(self.job("Just now", 30), match_score=90)
        self.assertFalse(evaluate_job(base, reference_time=self.REF, preferred_locations=[]).eligible)
        self.assertTrue(evaluate_job(dict(base, **self.job("Just now", 2)), reference_time=self.REF,
                                     preferred_locations=[]).eligible)


if __name__ == "__main__":
    unittest.main()
