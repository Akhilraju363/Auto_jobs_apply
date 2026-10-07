#!/usr/bin/env python3

import argparse
import logging
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

from config import (
    RESUME_PATH,
    DB_PATH,
    CHROME_USER_DATA,
    DAILY_LIMIT,
    GEMINI_API_KEY,
    NAUKRI_ENABLED,
    APPLICATION_JOB_WINDOW_HOURS,
    APPLICATION_SCORE_THRESHOLD,
    PREFERRED_LOCATIONS,
)
from db import init_db, get_applied_jobs_count
from resume_matcher import ResumeMatcher
from gemini_engine import GeminiEngine
from linkedin_driver import LinkedInDriver
from naukri_driver import NaukriDriver
from naukri_scanner import NaukriScanner, ScanResult
from ai_agent_bridge import export_discovered_jobs, import_agent_scores
from config import AI_AGENT_OUTPUT_DIR, APPLICATION_MAX_JOBS, NAUKRI_LOGIN_WAIT_MINUTES
from application_outcome import decide_idempotency
from db import (
    get_application_candidates,
    get_application_record,
    get_attempts,
    has_successful_application,
    open_attempts,
    get_job,
    get_jobs,
    get_jobs_by_application_state,
    set_application_state,
)
from naukri_application import (
    ApplicationRunner,
    ApplicationStop,
    NaukriApplicationBrowser,
    assess_job_page,
    find_tailored_resume,
    validate_resume_file,
)
from resume_artifact import resolve_verified_resume
from hitl_engine import HITLEngine
from eligibility import evaluate_job, priority_score, FRESH
from application_profile import ApplicationProfile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

active_drivers: list = []
hitl_engine: Optional[HITLEngine] = None


def setup_signal_handlers() -> None:
    """Setup graceful shutdown handlers for SIGINT (Ctrl+C)."""

    def signal_handler(sig, frame):
        logger.warning("Received interrupt signal, shutting down gracefully...")
        cleanup_and_exit(0)

    signal.signal(signal.SIGINT, signal_handler)


def cleanup_and_exit(exit_code: int = 0) -> None:
    """Clean up active resources and exit."""
    logger.info("Cleaning up resources...")

    for driver in active_drivers:
        try:
            driver.close()
            logger.debug(f"Closed driver: {driver.__class__.__name__}")
        except Exception as e:
            logger.warning(f"Error closing driver: {e}")

    active_drivers.clear()
    logger.info("Shutdown complete")
    sys.exit(exit_code)


def run_diagnostic_check() -> bool:
    """Run system health check and return True if all checks pass."""
    checks = {}

    # Check 1: Resume file
    try:
        if not RESUME_PATH.exists():
            checks["resume.txt"] = ("[-] Missing", False)
        elif RESUME_PATH.stat().st_size == 0:
            checks["resume.txt"] = ("[-] Empty", False)
        else:
            size = RESUME_PATH.stat().st_size
            checks["resume.txt"] = (f"[+] Found ({size} bytes)", True)
    except Exception as e:
        checks["resume.txt"] = (f"[-] Error: {e}", False)

    # Check 2: Database
    try:
        init_db()
        checks["Database"] = ("[+] Connected & Initialized", True)
    except Exception as e:
        checks["Database"] = (f"[-] Error: {str(e)[:50]}", False)

    # Check 3: Gemini API Key
    try:
        if not GEMINI_API_KEY:
            checks["GEMINI_API_KEY"] = ("[-] Not set in .env", False)
        else:
            try:
                gemini = GeminiEngine()
                prompt = "Say 'OK' in one word."
                response = gemini.model.generate_content(prompt)
                if response and response.text:
                    checks["GEMINI_API_KEY"] = ("[+] Valid & Responding", True)
                else:
                    checks["GEMINI_API_KEY"] = ("[-] Invalid response", False)
            except Exception as e:
                error_msg = str(e)[:40]
                checks["GEMINI_API_KEY"] = (f"[-] {error_msg}", False)
    except Exception as e:
        checks["GEMINI_API_KEY"] = (f"[-] Error: {str(e)[:40]}", False)

    # Check 4: Chrome User Data Directory
    try:
        CHROME_USER_DATA.mkdir(parents=True, exist_ok=True)
        checks["Chromium User Data"] = (f"[+] {CHROME_USER_DATA}", True)
    except Exception as e:
        checks["Chromium User Data"] = (f"[-] Error: {e}", False)

    # Check 5: Resume Matcher (model initialization)
    try:
        matcher = ResumeMatcher()
        text_len = len(matcher.resume_text or "")
        if text_len > 0:
            checks["Resume Matcher"] = (f"[+] Loaded ({text_len} chars)", True)
        else:
            checks["Resume Matcher"] = ("[*] Loaded (empty resume)", True)
    except Exception as e:
        checks["Resume Matcher"] = (f"[-] Error: {str(e)[:40]}", False)

    # Check 6: Chrome Extension Files
    try:
        from pathlib import Path

        # Look for extension in current directory first, then parent
        base_dir = Path(__file__).parent
        extension_dir = base_dir / "extension"

        if not extension_dir.exists():
            extension_dir = base_dir.parent / "extension"

        manifest_path = extension_dir / "manifest.json"

        if not extension_dir.exists():
            checks["Chrome Extension"] = (
                "[*] Optional - Not found (web dashboard will work without it)",
                True,
            )
        elif not manifest_path.exists():
            checks["Chrome Extension"] = (
                f"[*] Directory exists but manifest.json missing",
                True,
            )
        else:
            required_files = [
                "manifest.json",
                "popup.html",
                "popup.js",
                "content.js",
                "gemini.js",
                "background.js",
            ]
            missing_files = [f for f in required_files if not (extension_dir / f).exists()]

            if missing_files:
                checks["Chrome Extension"] = (
                    f"[*] Optional - Some files missing: {', '.join(missing_files)}",
                    True,
                )
            else:
                abs_path = extension_dir.absolute()
                checks["Chrome Extension"] = (
                    f"[+] Ready. Load: {abs_path}",
                    True,
                )
    except Exception as e:
        checks["Chrome Extension"] = (f"[*] Optional - Error: {str(e)[:40]}", True)

    # Print results
    print("\n" + "=" * 70)
    print("[*] SYSTEM DIAGNOSTIC CHECK")
    print("=" * 70)

    all_passed = True
    for check_name, (status, passed) in checks.items():
        symbol = "[+]" if passed else "[-]"
        padded_name = check_name.ljust(30)
        print(f"{symbol} {padded_name} {status}")
        if not passed:
            all_passed = False

    print("=" * 70)
    if all_passed:
        print("[+] All checks passed! Ready to run.")
    else:
        print("[-] Some checks failed. Please review the errors above.")
    print("=" * 70 + "\n")

    return all_passed


def run_automation(
    platform: str,
    headless: bool,
    limit: Optional[int],
) -> None:
    """Run job application automation."""
    global active_drivers, hitl_engine

    hitl_engine = HITLEngine()
    daily_limit = limit or DAILY_LIMIT

    logger.info("=" * 70)
    logger.info(
        f"Starting Automation | Platform: {platform} | Headless: {headless} | Limit: {daily_limit}"
    )
    logger.info("=" * 70)

    try:
        if platform in ["all", "linkedin"]:
            logger.info("Initializing LinkedIn Driver...")
            linkedin = LinkedInDriver(headless=headless, hitl_engine=hitl_engine)
            active_drivers.append(linkedin)

            result = linkedin.search_and_apply_jobs(max_limit=daily_limit)
            logger.info(f"LinkedIn Results: {result}")

            linkedin.close()
            active_drivers.remove(linkedin)

        if platform in ["all", "naukri"]:
            logger.info("Initializing Naukri Driver...")
            naukri = NaukriDriver(headless=headless, hitl_engine=hitl_engine)
            active_drivers.append(naukri)

            result = naukri.search_and_apply_jobs(max_limit=daily_limit)
            logger.info(f"Naukri Results: {result}")

            naukri.close()
            active_drivers.remove(naukri)

        logger.info("=" * 70)
        logger.info("Automation Completed Successfully")
        logger.info("=" * 70)

    except KeyboardInterrupt:
        logger.warning("Automation interrupted by user")
        cleanup_and_exit(1)
    except Exception as e:
        logger.error(f"Automation error: {e}", exc_info=True)
        cleanup_and_exit(1)


def run_naukri_scan(
    headless: bool,
    keywords: Optional[list[str]],
    max_pages: Optional[int],
    max_jobs: Optional[int],
) -> ScanResult:
    """Phase 1: discover Naukri jobs and store them with status='discovered'."""
    scanner = NaukriScanner(
        headless=headless, keywords=keywords, max_pages=max_pages, max_jobs=max_jobs
    )
    active_drivers.append(scanner)
    try:
        result = scanner.scan()
    except Exception as e:
        logger.error(f"Unrecoverable scanner failure: {e}", exc_info=True)
        cleanup_and_exit(1)
    finally:
        scanner.close()
        if scanner in active_drivers:
            active_drivers.remove(scanner)
    print_scan_summary(result)
    return result


def print_scan_summary(result: ScanResult) -> None:
    line = "=" * 40
    print(f"\n{line}\nNAUKRI JOB SCAN\n{line}\n")
    print("Keywords:")
    for keyword in result.keywords:
        print(f"- {keyword}")
    print()
    print(f"Pages scanned: {result.pages_scanned}")
    print(f"Job cards found: {result.cards_found}")
    print(f"Malformed cards: {result.malformed_cards}")
    print(f"Unique jobs: {result.unique_jobs}")
    print(f"New jobs stored: {result.new_jobs_stored}")
    print(f"Duplicates skipped: {result.duplicates_skipped}")
    print(f"Failed job pages: {result.failed_job_pages}")
    if result.stopped_reason:
        print(f"\nStopped early: {result.stopped_reason}")
    status = "SCAN STOPPED" if result.stopped_reason else "SCAN COMPLETE"
    print(f"\n{line}\n{status}\n{line}\n")


def run_export_to_agent(limit: Optional[int]) -> None:
    """Phase 2: write discovered jobs to the AI Agent handoff file (does not run the Agent)."""
    result = export_discovered_jobs(limit=limit)
    line = "=" * 40
    print(f"\n{line}\nNAUKRI -> AI AGENT EXPORT\n{line}\n")
    print(f"Discovered jobs: {result.discovered}")
    print(f"Eligible jobs: {result.eligible}")
    print(f"  (descriptions supplemented with Naukri key skills: {result.supplemented})")
    print(f"Skipped short/invalid jobs: {len(result.skipped)}")
    for reference, reason in result.skipped:
        print(f"  - {reference}: {reason}")
    print(f"Export limit: {result.limit}")
    print(f"Exported: {len(result.exported)}")
    print(f"\nOutput:\n{result.path}\n\n{line}\n")


def run_import_scores() -> None:
    """Phase 2: copy AI Agent scores (scored_jobs.json) into jobs.db."""
    result = import_agent_scores()
    line = "=" * 40
    print(f"\n{line}\nAI AGENT -> JOBS.DB SCORE IMPORT\n{line}\n")
    print(f"Source: {result.path}")
    print(f"AI Agent results read: {result.results_read}")
    print(f"Matched to jobs.db: {result.matched}")
    print(f"Newly scored: {result.scored_now}")
    for status, count in result.by_status.items():
        print(f"  {status}: {count}")
    print(f"Already scored (unchanged): {result.already_scored}")
    print(f"Malformed results skipped: {result.malformed}")
    print(f"Results for other sources/jobs: {result.unmatched}")
    print(f"Jobs still discovered: {result.still_discovered}")
    print(f"\n{line}\n")


def _print_candidate(job: dict) -> None:
    state = job.get("application_state") or "not prepared"
    print(f"  [{job['id']}] {int(job['match_score'] or 0)}/100 {job['match_status']:<11} "
          f"{job['title']} @ {job['company']} | {state}")


def _candidate_view(job: dict, include_review: bool = False) -> Optional[tuple[dict, object]]:
    if not include_review and job.get("match_status") != "recommended":
        return None
    artifact, _, _ = resolve_verified_resume(job["url"], AI_AGENT_OUTPUT_DIR)
    raw_experience = ApplicationProfile.from_environment().total_experience
    applicant_experience = None
    if raw_experience:
        try:
            applicant_experience = float(raw_experience.split()[0])
        except (ValueError, IndexError):
            applicant_experience = None
    result = evaluate_job(
        job,
        reference_time=datetime.now(timezone.utc),
        preferred_locations=PREFERRED_LOCATIONS,
        applicant_experience=applicant_experience,
        score_threshold=APPLICATION_SCORE_THRESHOLD,
        resume_ready=artifact is not None and artifact.verified,
        window_hours=APPLICATION_JOB_WINDOW_HOURS,
    )
    return job, result


def run_application_candidates(include_review: bool) -> None:
    """Phase 3: list scored jobs eligible for application preparation (no browser)."""
    init_db()
    candidates = []
    for job in get_application_candidates(include_review=include_review):
        view = _candidate_view(job, include_review)
        if view and view[1].eligible:
            candidates.append(view)
    candidates.sort(key=lambda item: (-priority_score(item[0], item[1]), item[0]["id"]))
    line = "=" * 50
    print(f"\n{line}\nAPPLICATION CANDIDATES ({'recommended + review' if include_review else 'recommended'})\n{line}")
    for rank, (job, result) in enumerate(candidates, 1):
        print(f"\n[{rank}] {job['id']} | {int(job['match_score'] or 0)}/100 "
              f"{result.freshness_status} | Priority {priority_score(job, result)}/100")
        print(f"{job['title']}\nCompany: {job['company']}")
        print(f"Location: {job.get('location') or '-'} | Location match: "
              f"{'YES' if result.location_match is True else 'UNKNOWN'}")
        print(f"Experience match: {'YES' if result.experience_match is True else 'UNKNOWN'}")
        print("Resume: VERIFIED" if result.resume_ready else "Resume: NOT READY")
        print(f"Application state: {job.get('application_state') or 'READY'}")
    print(f"\nCandidates: {len(candidates)}")
    print("Verify one: python main.py --action verify-job --job-id <id>")
    print(f"{line}\n")


def run_fresh_jobs(max_jobs: Optional[int]) -> None:
    """List fresh stored jobs without opening a browser or application form."""
    init_db()
    fresh = []
    for job in get_jobs(status=None, limit=None):
        result = evaluate_job(
            job, reference_time=datetime.now(timezone.utc),
            preferred_locations=PREFERRED_LOCATIONS,
            score_threshold=APPLICATION_SCORE_THRESHOLD,
            resume_ready=True, window_hours=APPLICATION_JOB_WINDOW_HOURS,
        )
        if result.freshness_status == FRESH:
            fresh.append((job, result))
    if max_jobs:
        fresh = fresh[:max_jobs]
    print("\nFRESH JOBS")
    for job, result in fresh:
        print(f"[{job['id']}] {job['title']} @ {job['company']} | "
              f"{job.get('posted_label') or job.get('posted_at') or '-'} | {result.freshness_status}")
    print(f"Jobs: {len(fresh)}\n")


def run_naukri_login() -> bool:
    """Phase 3: open the saved profile on Naukri's login page and wait while the user logs in."""
    try:
        with NaukriApplicationBrowser() as browser:
            ok = browser.wait_for_login(NAUKRI_LOGIN_WAIT_MINUTES)
    except ApplicationStop as e:
        logger.error(f"{e.code}: {e}")
        return False
    print("Naukri login detected; the session is saved in chrome_user_data/." if ok
          else "Login was not detected before the time ran out. Run the command again when ready.")
    return ok


def run_verify_job(job_row_id: int) -> int:
    """Phase 3: open one job with the saved profile and report its state. Never clicks Apply."""
    init_db()
    job = get_job(job_row_id)
    if job is None:
        logger.error(f"No job with id {job_row_id} in jobs.db")
        return 1
    resume, why = find_tailored_resume(job["url"], AI_AGENT_OUTPUT_DIR)
    if resume is not None:
        ok, why = validate_resume_file(resume.path)
        resume = resume if ok else None
    try:
        with NaukriApplicationBrowser() as browser:
            assessment = assess_job_page(browser.open_job(job["url"]), job["url"])
    except ApplicationStop as e:
        logger.error(f"{e.code}: {e}")
        return 1
    except Exception as e:
        logger.error(f"NETWORK_TIMEOUT: could not open {job['url']}: {e}")
        return 1
    if assessment.state != "ready":  # a 'ready' verification is not an approval; keep the stored state
        set_application_state(job["id"], assessment.state, assessment.code, assessment.reason, assessment.external_url)
    line = "=" * 50
    print(f"\n{line}\nVERIFY JOB [{job['id']}] {job['title']} @ {job['company']}\n{line}")
    print(f"URL: {job['url']}")
    print(f"Naukri state: {assessment.state}" + (f" ({assessment.code})" if assessment.code else ""))
    print(f"Detail: {assessment.reason}")
    if assessment.external_url:
        print(f"External application URL: {assessment.external_url}")
    print(f"Tailored resume: {resume.path if resume else 'NOT FOUND - ' + why}")
    if assessment.state == "requires_login":
        print("\nLog in once by hand: python main.py --action naukri-login")
    print(f"{line}\n")
    return 0 if assessment.state in ("ready", "already_applied", "external_application") else 1


def run_verify_resume(job_row_id: int) -> int:
    """Phase 3.2: show the verified job-specific resume artifact and whether it can be uploaded.
    Read-only (no browser, nothing generated or downloaded); never prints resume contents."""
    init_db()
    job = get_job(job_row_id)
    if job is None:
        logger.error(f"No job with id {job_row_id} in jobs.db")
        return 1
    artifact, code, reason = resolve_verified_resume(job["url"], AI_AGENT_OUTPUT_DIR)
    line = "=" * 50
    print(f"\n{line}\nRESUME ARTIFACT\n{line}\n\nJob: {job['title']}\nCompany: {job['company']}\n")
    if artifact is None:
        print(f"Verified: NO\nResult: {code}\nDetail: {reason}\n{line}\n")
        return 1
    print(f"Verified: YES\nVersion: v{artifact.version}\nResume record: {artifact.resume_id}\n")
    print(f"Markdown:\n{artifact.markdown_path}\n")
    print(f"PDF:\n{artifact.pdf_path or 'not published'}\n")
    if artifact.docx_path:
        print(f"DOCX:\n{artifact.docx_path}\n")
    print(f"PDF Valid: {'YES' if artifact.uploadable and artifact.upload_path.suffix == '.pdf' else 'NO'}")
    print(f"Job Match: {'NO' if artifact.upload_code == 'RESUME_ARTIFACT_MISMATCH' else 'YES'}")
    print(f"Upload candidate: {artifact.upload_path or 'none'}")
    for check in artifact.checks:
        print(f"  - {check}")
    if artifact.upload_code:
        print(f"Not uploadable: {artifact.upload_code} - {artifact.upload_reason}")
        if artifact.upload_code == "RESUME_PDF_NOT_FOUND":
            print("Publish it from the AI Agent folder:\n"
                  f"  python scripts/resume_artifacts.py --resume-id {artifact.resume_id}")
    print(f"{line}\n")
    return 0 if artifact.uploadable else 1


PROBLEM_STATES = ("recovery_required", "review_required", "ready", "failed", "requires_login",
                  "requires_manual_action", "approved", "submitting")


def _print_job_status(job: dict) -> None:
    attempts = get_attempts(job["id"])
    last = attempts[-1] if attempts else None
    record = get_application_record(job)
    artifact, code, _ = resolve_verified_resume(job["url"], AI_AGENT_OUTPUT_DIR)
    resume = (f"{(artifact.upload_path or artifact.markdown_path).name} (v{artifact.version}"
              f"{', uploadable' if artifact.uploadable else ', not uploadable: ' + str(artifact.upload_code)})"
              if artifact else f"none ({code})")
    print(f"Job ID: {job['id']}  (Naukri {job.get('job_id') or '-'})")
    print(f"Title: {job['title']}\nCompany: {job['company']}")
    print(f"State: {(job.get('application_state') or 'not prepared').upper()}")
    print(f"Code: {job.get('application_code') or '-'}")
    print(f"Last Updated: {job.get('application_updated_at') or '-'}")
    if last:
        print(f"Last attempt: #{last['id']} {last['kind']} {last['started_at']} -> {last['state']}"
              f" ({last['code'] or '-'}), Apply clicked: {'yes' if last['apply_clicked'] else 'no'}"
              f"{', confirmed by ' + last['confirmation_signal'] if last['confirmation_signal'] else ''}")
    print(f"Attempts recorded: {len(attempts)}")
    print(f"Application history: {record['status'] if record else 'none'}")
    print(f"Resume: {resume}")
    print(f"Score: {int(job.get('match_score') or 0)}/100 ({job.get('match_status') or 'unscored'})")
    print(f"Last known URL: {job['url']}")
    if job.get("external_apply_url"):
        print(f"External URL: {job['external_apply_url']}")
    print(f"Reason: {job.get('application_reason') or '-'}")
    decision = decide_idempotency(job, has_successful_application(job), record, open_attempts(job["id"]))
    if decision.proceed:
        print("Idempotency: a new application may be prepared (approval still required)")
    else:
        print(f"Idempotency: BLOCKED -> {decision.state} ({decision.code}): {decision.reason}")
    if job.get("application_state") == "recovery_required" or decision.state == "recovery_required":
        print(f"Next: python main.py --action recover-application --job-id {job['id']}")


def run_application_status(job_row_id: Optional[int], max_jobs: Optional[int]) -> int:
    """Phase 3.3: read-only status of one job, or of every job that needs attention. Never acts."""
    init_db()
    line = "=" * 50
    if job_row_id is not None:
        job = get_job(job_row_id)
        if job is None:
            logger.error(f"No job with id {job_row_id} in jobs.db")
            return 1
        print(f"\n{line}\nAPPLICATION STATUS\n{line}")
        _print_job_status(job)
        print(f"{line}\n")
        return 0
    jobs = get_jobs_by_application_state(PROBLEM_STATES, limit=max_jobs)
    print(f"\n{line}\nAPPLICATIONS NEEDING ATTENTION\n{line}")
    for state in PROBLEM_STATES:
        group = [j for j in jobs if j.get("application_state") == state]
        if group:
            print(f"\n{state.upper()} ({len(group)})")
            for job in group:
                print(f"  [{job['id']}] {job['title']} @ {job['company']} | {job.get('application_code') or '-'}"
                      f" | {job.get('application_updated_at') or '-'}")
    if not jobs:
        print("\nNone.")
    print(f"\nDetails: python main.py --action application-status --job-id <id>\n{line}\n")
    return 0


def run_recover_application(job_row_id: int) -> int:
    """Phase 3.3: inspect Naukri for one job (read-only) and settle its state with the user."""
    init_db()
    job = get_job(job_row_id)
    if job is None:
        logger.error(f"No job with id {job_row_id} in jobs.db")
        return 1
    line = "=" * 50
    print(f"\n{line}\nRECOVER APPLICATION\n{line}")
    _print_job_status(job)
    print(f"{line}\nOpening the job with your saved Naukri session (nothing will be clicked or submitted)...")
    try:
        with NaukriApplicationBrowser() as browser:
            result = ApplicationRunner(browser).recover(job)
    except ApplicationStop as e:
        print(f"\nSTOPPED ({e.code}): {e}\nThe job stays in recovery.")
        return 1
    print(f"\nResult: {result.state.upper()}" + (f" ({result.code})" if result.code else ""))
    print(f"Reason: {result.reason}")
    if result.state == "ready":
        print(f"Next: python main.py --action apply --job-id {job_row_id} --dry-run, then without --dry-run "
              "(you will be asked to approve again).")
    return 0 if result.state != "recovery_required" else 1


def run_apply(job_row_id: Optional[int], dry_run: bool, max_jobs: Optional[int], include_review: bool) -> int:
    """Phase 3: prepare applications one by one; every submission needs an explicit 'y'."""
    init_db()
    limit = max_jobs or APPLICATION_MAX_JOBS
    stored_candidates = get_application_candidates(
        include_review=include_review, job_row_id=job_row_id, limit=limit
    )
    candidates = []
    for candidate in stored_candidates:
        view = _candidate_view(candidate, include_review)
        if view and view[1].eligible:
            candidates.append(candidate)
        else:
            result = view[1] if view else None
            print(f"[{candidate['id']}] not eligible: {result.reason if result else 'review-only score'}")
    if not candidates:
        if job_row_id is not None and (job := get_job(job_row_id)):
            print(f"Job {job_row_id} is not an application candidate (status={job['status']}, "
                  f"match_status={job['match_status']}, application_state={job.get('application_state')}).")
        else:
            print("No application candidates. See: python main.py --action application-candidates")
        return 0 if job_row_id is None else 1
    print(f"{'DRY RUN - ' if dry_run else ''}Preparing {len(candidates)} application(s); "
          "each one needs your approval before Apply is clicked.")
    if dry_run:
        for candidate in candidates:
            result = _candidate_view(candidate, include_review)[1]
            print(f"[{candidate['id']}] freshness={result.freshness_status} "
                  f"score={result.score:g}/10 location={result.location_match} "
                  f"experience={result.experience_match} resume={result.resume_ready} "
                  f"history={candidate.get('application_state') or 'none'} "
                  f"decision={'ELIGIBLE' if result.eligible else 'BLOCKED'} reason={result.reason}")
    results = []
    with NaukriApplicationBrowser() as browser:
        runner = ApplicationRunner(browser, dry_run=dry_run)
        for job in candidates:
            try:
                results.append(runner.process(job))
            except ApplicationStop as e:
                print(f"\nSTOPPED ({e.code}): {e}")
                break
            result = results[-1]
            print(f"-> [{job['id']}] {result.state}" + (f" ({result.code})" if result.code else "")
                  + (f": {result.reason}" if result.reason else ""))
            if result.external_url:
                print(f"   Apply manually at: {result.external_url}")
    submitted = sum(1 for r in results if r.submitted)
    print(f"\nProcessed: {len(results)} | Submitted: {submitted}" + (" | DRY RUN: nothing clicked or recorded" if dry_run else ""))
    if submitted:
        print("Tracker: set the AI Agent's Google Sheet status to Applied for these jobs (not automated yet).")
    return 0


def run_web_server() -> None:
    """Start Flask web dashboard."""
    logger.info("=" * 70)
    logger.info("[WEB] Starting Flask Dashboard Server")
    logger.info("=" * 70)
    logger.info("[>] Dashboard: http://localhost:5000")
    logger.info("[>] Press Ctrl+C to stop the server")
    logger.info("=" * 70)

    try:
        from app import app

        app.run(debug=False, port=5000, threaded=True)
    except KeyboardInterrupt:
        logger.info("Web server stopped by user")
        cleanup_and_exit(0)
    except Exception as e:
        logger.error(f"Web server error: {e}", exc_info=True)
        cleanup_and_exit(1)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Job Application Automation Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --mode web                          # Start web dashboard
  python main.py --mode cli --platform all           # Run automation in CLI
  python main.py --mode test                         # Run system diagnostics
  python main.py --mode cli --platform linkedin      # LinkedIn only
  python main.py --mode cli --headless --limit 10   # Headless mode with limit
  python main.py --platform naukri --action scan     # Discover Naukri jobs (no applying)
  python main.py --platform naukri --action scan --max-pages 1 --max-jobs 5
  python main.py --action export-to-agent            # Write discovered jobs for the AI Agent
  python main.py --action import-scores              # Read AI Agent scores into jobs.db
        """,
    )

    parser.add_argument(
        "--mode",
        choices=["web", "cli", "test"],
        default="web",
        help="Execution mode: web (Flask dashboard), cli (terminal), test (diagnostics)",
    )

    parser.add_argument(
        "--platform",
        choices=["all", "linkedin", "naukri"],
        default="all",
        help="Job platforms to target (default: all)",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Playwright in headless mode (default: headed for manual login)",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"Override daily application limit (default: {DAILY_LIMIT})",
    )

    parser.add_argument(
        "--action",
        choices=[
            "scan",
            "fresh-jobs",
            "export-to-agent",
            "import-scores",
            "application-candidates",
            "naukri-login",
            "verify-job",
            "verify-resume",
            "apply",
            "application-status",
            "recover-application",
        ],
        default=None,
        help=        "scan (Naukri discovery), fresh-jobs, export-to-agent / import-scores (AI Agent file handoff), "
        "application-candidates / naukri-login / verify-job / apply (Phase 3, human-approved applications). "
        "Any --action runs regardless of --mode; without --action, --mode decides (legacy behaviour)",
    )

    parser.add_argument("--job-id", type=int, default=None, help="jobs.db row id for verify-job / apply")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="apply: open and check the job, show the review, never click Apply or write to the database",
    )
    parser.add_argument(
        "--include-review",
        action="store_true",
        help="application-candidates / apply: also include match_status 'review' jobs (default: recommended only)",
    )

    parser.add_argument(
        "--keywords",
        default=None,
        help="Comma-separated search keywords for --action scan (default: NAUKRI_KEYWORDS)",
    )

    parser.add_argument("--max-pages", type=int, default=None, help="Scan: max result pages per keyword")
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="Scan: max new jobs to process. export-to-agent: max jobs to export (default: AI_AGENT_EXPORT_LIMIT). "
        "apply: max jobs this run (default: APPLICATION_MAX_JOBS)",
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    # Configure logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Setup signal handlers for graceful shutdown
    setup_signal_handlers()

    logger.info("=" * 70)
    logger.info("[*] Job Application Automation Engine")
    logger.info("=" * 70)

    # Route to appropriate execution mode
    if args.action == "scan":
        if args.platform == "linkedin":
            parser.error("--action scan currently supports only --platform naukri")
        if not NAUKRI_ENABLED:
            logger.warning("Naukri scanning is disabled (NAUKRI_ENABLED=false)")
            cleanup_and_exit(0)
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()] if args.keywords else None
        result = run_naukri_scan(args.headless, keywords, args.max_pages, args.max_jobs)
        cleanup_and_exit(1 if result.stopped_reason and not result.new_jobs_stored else 0)

    elif args.action == "fresh-jobs":
        run_fresh_jobs(args.max_jobs)
        cleanup_and_exit(0)

    elif args.action == "export-to-agent":
        run_export_to_agent(args.max_jobs)
        cleanup_and_exit(0)

    elif args.action == "import-scores":
        try:
            run_import_scores()
        except (ValueError, FileNotFoundError) as e:
            logger.error(f"Score import failed: {e}")
            cleanup_and_exit(1)
        cleanup_and_exit(0)

    elif args.action == "application-candidates":
        run_application_candidates(args.include_review)
        cleanup_and_exit(0)

    elif args.action == "naukri-login":
        cleanup_and_exit(0 if run_naukri_login() else 1)

    elif args.action == "verify-job":
        if args.job_id is None:
            parser.error("--action verify-job requires --job-id <jobs.db id>")
        cleanup_and_exit(run_verify_job(args.job_id))

    elif args.action == "verify-resume":
        if args.job_id is None:
            parser.error("--action verify-resume requires --job-id <jobs.db id>")
        cleanup_and_exit(run_verify_resume(args.job_id))

    elif args.action == "application-status":
        cleanup_and_exit(run_application_status(args.job_id, args.max_jobs))

    elif args.action == "recover-application":
        if args.job_id is None:
            parser.error("--action recover-application requires --job-id <jobs.db id>")
        cleanup_and_exit(run_recover_application(args.job_id))

    elif args.action == "apply":
        cleanup_and_exit(run_apply(args.job_id, args.dry_run, args.max_jobs, args.include_review))

    elif args.mode == "test":
        logger.info("Running System Diagnostic Check...")
        all_passed = run_diagnostic_check()
        cleanup_and_exit(0 if all_passed else 1)

    elif args.mode == "web":
        run_web_server()

    elif args.mode == "cli":
        run_automation(
            platform=args.platform,
            headless=args.headless,
            limit=args.limit,
        )
        cleanup_and_exit(0)


if __name__ == "__main__":
    main()
