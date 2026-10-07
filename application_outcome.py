"""Deterministic application outcome rules (Phase 3.3): confirmation and idempotency.

APPLIED means verified application confirmation, not merely a click. Confirmation needs an
explicit Naukri signal: a strong success message, or Naukri's own "Applied" control in the job
header. A URL change, a vanished Submit button or a finished network request is never
confirmation. Ambiguous submission is never automatically retried: once Apply may have been
clicked, the job needs recovery (recover-application) before anything else happens to it.
No LLM, no heuristics beyond fixed text and DOM facts.
"""

import re
from dataclasses import dataclass
from typing import Optional

# Codes added in Phase 3.3 (stored in jobs.application_code / application_attempts.code).
SUBMISSION_UNCONFIRMED = "SUBMISSION_UNCONFIRMED"  # Apply/Submit clicked, no confirmation
INTERRUPTED_AFTER_APPLY = "INTERRUPTED_AFTER_APPLY"  # run ended (crash, Ctrl+C) after Apply was clicked
INTERRUPTED_BEFORE_APPLY = "INTERRUPTED_BEFORE_APPLY"  # run ended before any click: nothing submitted
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
RECOVERED_NOT_APPLIED = "RECOVERED_NOT_APPLIED"  # recovery saw Naukri's Apply button and the user requeued it
CONFIRMED_DURING_RECOVERY = "CONFIRMED_DURING_RECOVERY"

# Strong, explicit success messages only. ("you have applied" alone also appears in navigation
# like "Jobs you have applied to", so it is deliberately not here.)
SUCCESS_MESSAGES = (
    "successfully applied",
    "applied successfully",
    "application submitted successfully",
    "your application has been submitted",
    "application sent successfully",
    "you have successfully applied",
)
FAILURE_MESSAGES = (
    "something went wrong",
    "unable to apply",
    "could not apply",
    "failed to apply",
    "application failed",
)
APPLIED_TEXTS = ("applied", "already applied")
# Earlier phases may have stopped after Apply was clicked without leaving an attempt record;
# these codes on a not-applied job only happen after Apply.
POST_APPLY_CODES = (
    "UNSUPPORTED_QUESTION", "QUESTION_MANUAL_REQUIRED", "UNCONFIGURED_ANSWER", "QUESTION_FORM_CHANGED",
    "QUESTION_INPUT_NOT_FOUND", "QUESTION_SAVE_NOT_FOUND", "QUESTION_AUTO_ANSWERED", "SUBMISSION_FAILED",
    "RESUME_UPLOAD_FORM_CHANGED", "RESUME_UPLOAD_NOT_CONFIRMED", "RESUME_UPLOADED", SUBMISSION_UNCONFIRMED,
    INTERRUPTED_AFTER_APPLY,
)
UNRESOLVED_RECORD_STATUSES = ("unconfirmed", "incomplete", "submission_failed")


@dataclass(frozen=True)
class ConfirmationResult:
    confirmed: bool
    signal: Optional[str] = None  # success_message | applied_state
    reason: Optional[str] = None
    failed: bool = False  # Naukri explicitly reported a failure


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def applied_control_in_header(buttons: list) -> bool:
    """Naukri's own Applied control. Only controls in the job header count (in_job_header is
    reported by the live snapshot; synthetic snapshots without it are treated as header buttons)."""
    for button in buttons or []:
        if not isinstance(button, dict):
            continue
        text = _norm(button.get("text"))
        if (str(button.get("id") or "").lower() == "already-applied" or text in APPLIED_TEXTS) \
                and button.get("in_job_header", True):
            return True
    return False


def detect_application_confirmation(snapshot: dict) -> ConfirmationResult:
    """Is the application confirmed, by explicit Naukri signals only?"""
    body = _norm((snapshot or {}).get("body", ""))
    message = next((m for m in SUCCESS_MESSAGES if m in body), None)
    if message:
        return ConfirmationResult(True, "success_message", f"Naukri says '{message}'")
    if applied_control_in_header((snapshot or {}).get("buttons")):
        return ConfirmationResult(True, "applied_state", "Naukri shows the job as Applied")
    failure = next((m for m in FAILURE_MESSAGES if m in body), None)
    if failure:
        return ConfirmationResult(False, None, f"Naukri reported '{failure}'", failed=True)
    return ConfirmationResult(False, None, "no Naukri confirmation signal")


@dataclass(frozen=True)
class IdempotencyDecision:
    proceed: bool
    state: Optional[str] = None
    code: Optional[str] = None
    reason: str = ""
    close_open_attempts: tuple = ()  # (attempt id, state, code) to close before proceeding/stopping


def decide_idempotency(job: dict, successful: bool, record: Optional[dict], open_attempts: list[dict]
                       ) -> IdempotencyDecision:
    """May a new application be prepared for this job? Pure: the caller applies the decision."""
    if successful:
        return IdempotencyDecision(False, "already_applied", "ALREADY_APPLIED", "already recorded as applied")
    clicked = [a for a in open_attempts if a.get("apply_clicked")]
    not_clicked = tuple((a["id"], "interrupted", INTERRUPTED_BEFORE_APPLY) for a in open_attempts if not a.get("apply_clicked"))
    if clicked:
        closes = not_clicked + tuple((a["id"], "recovery_required", INTERRUPTED_AFTER_APPLY) for a in clicked)
        return IdempotencyDecision(False, "recovery_required", INTERRUPTED_AFTER_APPLY,
                                   "a previous run ended after Apply was clicked; the outcome is unknown", closes)
    if job.get("application_state") == "recovery_required":
        return IdempotencyDecision(False, "recovery_required", job.get("application_code") or RECOVERY_REQUIRED,
                                   job.get("application_reason") or "recovery required", not_clicked)
    if record and record.get("status") in UNRESOLVED_RECORD_STATUSES:
        return IdempotencyDecision(False, "recovery_required", SUBMISSION_UNCONFIRMED,
                                   f"an earlier attempt is '{record['status']}': it may have been submitted", not_clicked)
    if job.get("application_state") in ("approved", "submitting") or (
            job.get("application_state") in ("requires_manual_action", "failed")
            and job.get("application_code") in POST_APPLY_CODES):
        return IdempotencyDecision(False, "recovery_required", INTERRUPTED_AFTER_APPLY,
                                   "an earlier run reached the application form; its outcome is unknown", not_clicked)
    return IdempotencyDecision(True, close_open_attempts=not_clicked)


__all__ = [
    "CONFIRMED_DURING_RECOVERY",
    "ConfirmationResult",
    "INTERRUPTED_AFTER_APPLY",
    "INTERRUPTED_BEFORE_APPLY",
    "IdempotencyDecision",
    "RECOVERED_NOT_APPLIED",
    "RECOVERY_REQUIRED",
    "SUBMISSION_UNCONFIRMED",
    "applied_control_in_header",
    "decide_idempotency",
    "detect_application_confirmation",
]
