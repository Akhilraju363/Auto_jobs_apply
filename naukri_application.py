"""Phase 3: Naukri application preparation with a human approval gate.

recommended job -> resume check -> open job (saved headed profile) -> verify login / availability /
apply state -> review -> explicit human approval -> click Naukri's own Apply -> handle the result.

Safety rules (do not relax):
- Login only through the user's manually authenticated chrome_user_data/ profile. No credentials,
  no OTP handling, no CAPTCHA solving: a block or uncertain login stops the job.
- A recommended score is not permission to submit. Naukri's in-platform Apply can submit on the
  first click, so approval is asked immediately before that click, for every job.
- Only Naukri's identified apply control is clicked; ambiguous pages go to the human.
- Questions are answered only from configured facts (APPLICANT_* in .env); anything else goes
  to the human. Free text is never generated.
- External (company-site) applications are reported, never clicked.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import application_questions as questions
from application_audit import build_diagnostics, log_event, write_diagnostics
from application_outcome import (
    CONFIRMED_DURING_RECOVERY,
    RECOVERED_NOT_APPLIED,
    SUBMISSION_UNCONFIRMED,
    ConfirmationResult,
    applied_control_in_header,
    decide_idempotency,
    detect_application_confirmation,
)
from application_profile import ApplicationProfile, ResumeEvidence
from config import (
    AI_AGENT_OUTPUT_DIR,
    CONFIRMATION_WAIT_SECONDS,
    NAUKRI_APPLICATION_TIMEOUT,
    NAUKRI_NAVIGATION_RETRIES,
)
from db import (
    finish_attempt,
    get_application_record,
    has_successful_application,
    mark_apply_clicked,
    open_attempts,
    record_application_attempt,
    resolve_unconfirmed_record,
    set_application_state,
    start_attempt,
)
from naukri_parser import detect_block, is_login_page
from resume_artifact import RESUME_ARTIFACT_MISMATCH, RESUME_PDF_NOT_FOUND, resolve_verified_resume

logger = logging.getLogger(__name__)

# Explicit outcome codes (stored in jobs.application_code).
AUTH_REQUIRED = "AUTH_REQUIRED"
CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
JOB_CLOSED = "JOB_CLOSED"
ALREADY_APPLIED = "ALREADY_APPLIED"
EXTERNAL_APPLICATION = "EXTERNAL_APPLICATION"
RESUME_NOT_FOUND = "RESUME_NOT_FOUND"
UNSUPPORTED_QUESTION = "UNSUPPORTED_QUESTION"
APPLICATION_FORM_CHANGED = "APPLICATION_FORM_CHANGED"
SUBMISSION_FAILED = "SUBMISSION_FAILED"
NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
UNKNOWN_UI_STATE = "UNKNOWN_UI_STATE"
BROWSER_PROFILE_LOCKED = "BROWSER_PROFILE_LOCKED"
NOT_APPROVED = "NOT_APPROVED"
APPLIED = "APPLIED"

UPLOADABLE_RESUME_TYPES = (".pdf", ".doc", ".docx")
REVIEWABLE_RESUME_TYPES = UPLOADABLE_RESUME_TYPES + (".md",)

_UNAVAILABLE_MARKERS = (
    "no longer available",
    "job has expired",
    "this job has expired",
    "no longer accepting applications",
    "job is closed",
    "position has been filled",
)
_NOT_FOUND_MARKERS = ("job not found", "page not found", "page you are looking for")
class ApplyClickUncertain(Exception):
    """The Apply click was attempted but did not complete cleanly: it may have submitted.
    (Not a RuntimeError: RuntimeErrors from the resolver mean Apply was provably not clicked.)"""


class ApplicationStop(Exception):
    """Stop the whole run (login missing, CAPTCHA not cleared, profile locked, user quit)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Assessment:
    state: str  # one of db.APPLICATION_STATES
    code: Optional[str] = None
    reason: str = ""
    external_url: Optional[str] = None


@dataclass
class ResumeInfo:
    path: Path  # the validated upload file when uploadable, else the verified markdown (review only)
    uploadable: bool
    drive_link: Optional[str] = None
    resume_id: Optional[str] = None
    version: Optional[int] = None
    markdown_path: Optional[Path] = None
    upload_code: Optional[str] = None
    upload_reason: str = ""


@dataclass
class ApplicationResult:
    job_row_id: int
    state: str
    code: Optional[str] = None
    reason: str = ""
    submitted: bool = False
    external_url: Optional[str] = None
    resume: Optional[ResumeInfo] = None
    notes: list[str] = field(default_factory=list)
    confirmation_signal: Optional[str] = None
    diagnostics_path: Optional[Path] = None


# ---------------------------------------------------------------------------
# Pure decisions (unit tested)
# ---------------------------------------------------------------------------


def classify_auth(snapshot: dict) -> str:
    """'authenticated' only with positive evidence; uncertain is never treated as logged in."""
    if is_login_page(snapshot.get("url", "")) or snapshot.get("login_links", 0) > 0:
        return "login_required"
    if snapshot.get("logged_in_markers", 0) > 0:
        return "authenticated"
    return "unknown"


def _button_flags(buttons: list) -> dict:
    flags = {"easy_apply": False, "external": False, "applied": False, "login_to_apply": False, "external_url": None}
    for button in buttons or []:
        element_id = str(button.get("id") or "").lower()
        text = re.sub(r"\s+", " ", str(button.get("text") or "")).strip().lower()
        if element_id == "already-applied" or text in ("applied", "already applied"):
            # only Naukri's own control in the job header counts (not a nav link reading "Applied")
            flags["applied"] = flags["applied"] or applied_control_in_header([button])
        elif element_id == "company-site-button" or "company site" in text:
            flags["external"] = True
            flags["external_url"] = flags["external_url"] or (button.get("href") or None)
        elif text in ("login to apply", "register to apply"):
            flags["login_to_apply"] = True
        elif element_id == "apply-button" and text in ("apply", "easy apply", "apply now") and not button.get("disabled"):
            flags["easy_apply"] = True
    return flags


def assess_job_page(snapshot: dict, job_url: str) -> Assessment:
    """Decide what a job page allows, from a read-only snapshot. Order matters: safety first."""
    url, body = snapshot.get("url", ""), str(snapshot.get("body", "")).lower()
    block = "captcha iframe" if snapshot.get("has_captcha_frame") else detect_block(snapshot.get("title", ""), url, body)
    if block:
        return Assessment("requires_manual_action", CAPTCHA_REQUIRED, f"Naukri verification/block page ({block})")
    auth = classify_auth(snapshot)
    if auth != "authenticated":
        detail = "Naukri login page or Login link shown" if auth == "login_required" else "login state could not be confirmed"
        return Assessment("requires_login", AUTH_REQUIRED, detail)
    if any(marker in body for marker in _UNAVAILABLE_MARKERS):
        return Assessment("job_unavailable", JOB_CLOSED, "Naukri says the job is closed or expired")
    if "job-listings" not in url or not snapshot.get("has_job_header") or any(m in body for m in _NOT_FOUND_MARKERS):
        return Assessment("job_unavailable", JOB_NOT_FOUND, f"job page not found (landed on {url})")
    flags = _button_flags(snapshot.get("buttons"))
    if flags["applied"]:
        return Assessment("already_applied", ALREADY_APPLIED, "Naukri shows this job as applied")
    if flags["easy_apply"] and flags["external"]:
        return Assessment("requires_manual_action", UNKNOWN_UI_STATE, "both Apply and company-site buttons shown")
    if flags["external"]:
        return Assessment("external_application", EXTERNAL_APPLICATION, "apply on the company's own site",
                          flags["external_url"] or job_url)
    if flags["easy_apply"]:
        return Assessment("ready", None, "Naukri Apply button available")
    if flags["login_to_apply"]:
        return Assessment("requires_login", AUTH_REQUIRED, "Naukri asks to log in before applying")
    return Assessment("requires_manual_action", UNKNOWN_UI_STATE, "no recognisable Naukri apply control")


def _as_profile(answers) -> ApplicationProfile:
    if isinstance(answers, ApplicationProfile):
        return answers
    return ApplicationProfile.from_mapping(answers or {})


def classify_question(text: str) -> Optional[str]:
    """The configured fact a question asks for, or None when it must be answered by the human.
    (Compatibility wrapper over application_questions.classify_question.)"""
    category = questions.classify_question(text)
    return category.value if category in questions.FACTUAL else None


def plan_answers(form_questions: list[dict], answers, evidence: Optional[ResumeEvidence] = None
                 ) -> tuple[list[tuple[dict, str]], list[tuple[dict, str]]]:
    """(answerable [(question, value)], unsupported [(question, reason)]) -- never invents a value."""
    profile = _as_profile(answers)
    planned, unsupported = [], []
    for question in form_questions:
        decision = questions.decide_answer(question.get("label", ""), profile, evidence,
                                           question.get("type", "text"), question.get("options") or None)
        if decision.automatic:
            planned.append((question, decision.value))
        else:
            unsupported.append((question, decision.reason))
    return planned, unsupported


APPLY_CONTROL_SELECTOR = "#apply-button"
APPLY_TEXTS = ("apply", "easy apply", "apply now")
# Naukri renders the same #apply-button twice: in the job header (section#job_header) and in a
# sticky header that only appears after scrolling. Both submit the same application, but once
# scrolled both report visible, so the job header copy is the deterministic choice.
APPLY_CONTEXT_JS = """
el => ({
  in_job_header: !!el.closest('#job_header'),
  in_sticky: !!el.closest("[class*='sticky' i]"),
})
"""


def choose_apply_control(candidates: list[dict]) -> tuple[Optional[int], str]:
    """Pick the one actionable Apply control from described #apply-button elements.

    Actionable = visible, enabled and reading Apply. One actionable -> that one. Several -> only
    the single one inside the job header and not in a sticky header. Anything else -> None
    (the caller reports APPLICATION_FORM_CHANGED and clicks nothing).
    """
    actionable = [c for c in candidates
                  if c.get("visible") and c.get("enabled") and str(c.get("text", "")).strip().lower() in APPLY_TEXTS]
    if not actionable:
        return None, f"no visible, enabled Apply control among {len(candidates)} #apply-button element(s)"
    if len(actionable) == 1:
        return actionable[0]["index"], ""
    in_header = [c for c in actionable if c.get("in_job_header") and not c.get("in_sticky")]
    if len(in_header) == 1:
        return in_header[0]["index"], ""
    return None, f"{len(actionable)} actionable Apply controls and no single job-header control"


# --- Naukri chat-style recruiter questions ------------------------------------------------------

QUESTION_FORM_CHANGED = "QUESTION_FORM_CHANGED"
QUESTION_INPUT_NOT_FOUND = "QUESTION_INPUT_NOT_FOUND"
QUESTION_SAVE_NOT_FOUND = "QUESTION_SAVE_NOT_FOUND"
MAX_CHAT_QUESTIONS = 25

# Describes the visible chat panel and tags candidate elements with data-aja markers so the
# actions below touch exactly the elements that were described (stale markers fail closed).
CHAT_DESCRIBE_JS = """
() => {
  const visible = e => !!(e && (e.offsetParent !== null || e.getClientRects().length) &&
                          getComputedStyle(e).visibility !== 'hidden');
  document.querySelectorAll('[data-aja]').forEach(e => e.removeAttribute('data-aja'));
  const found = Array.from(document.querySelectorAll("[class*='chatbot' i]")).filter(visible);
  const roots = found.filter(c => !found.some(o => o !== c && o.contains(c)));
  if (!roots.length) return { present: false };
  if (roots.length > 1) return { present: true, roots: roots.length };
  const root = roots[0];
  const textOf = e => (e.innerText || '').replace(/\\s+/g, ' ').trim();
  const innermost = list => list.filter(e => !list.some(o => o !== e && e.contains(o)));
  const msgs = innermost(Array.from(root.querySelectorAll(
    "[class*='botMsg' i], [class*='bot-msg' i], [class*='botItem' i]")).filter(visible));
  const fields = Array.from(root.querySelectorAll("[contenteditable='true'], textarea, input")).filter(e => {
    const type = (e.getAttribute('type') || '').toLowerCase();
    if (!visible(e) || e.disabled || e.readOnly) return false;
    return e.isContentEditable || !['hidden', 'radio', 'checkbox', 'submit', 'button', 'file'].includes(type);
  });
  fields.forEach((e, i) => e.setAttribute('data-aja', 'input-' + i));
  const labelFor = el => {
    if (el.id) { const l = root.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l; }
    return el.closest('label');
  };
  const options = [];
  Array.from(root.querySelectorAll("input[type='radio']")).forEach((el, i) => {
    const label = labelFor(el);
    const target = label && visible(label) ? label : (visible(el) ? el : null);
    if (!target) return;
    target.setAttribute('data-aja', 'option-' + i);
    options.push({ ref: 'option-' + i, text: textOf(label || el) || el.value || '' });
  });
  const checkboxes = Array.from(root.querySelectorAll("input[type='checkbox']"))
    .filter(e => visible(e) || (labelFor(e) && visible(labelFor(e)))).length;
  const clickable = Array.from(root.querySelectorAll("button, a, [role='button'], div, span")).filter(visible);
  const pick = re => innermost(clickable.filter(e => re.test(textOf(e))));
  const saves = pick(/^(save|submit|next|send|continue)$/i);
  saves.forEach((e, i) => e.setAttribute('data-aja', 'save-' + i));
  const skips = pick(/^skip( this question)?$/i);
  return {
    present: true, roots: 1,
    question: msgs.length ? textOf(msgs[msgs.length - 1]) : '',
    message_count: msgs.length,
    inputs: fields.map((e, i) => ({ ref: 'input-' + i,
      kind: e.isContentEditable ? 'contenteditable' : e.tagName === 'TEXTAREA' ? 'textarea' : (e.getAttribute('type') || 'text') })),
    options: options, checkbox_count: checkboxes,
    saves: saves.map((e, i) => ({ ref: 'save-' + i, text: textOf(e) })),
    skip_count: skips.length,
    file_count: root.querySelectorAll("input[type='file']").length,
  };
}
"""


# --- Resume upload (Phase 3.2) ---------------------------------------------------------------------

RESUME_UPLOAD_FORM_CHANGED = "RESUME_UPLOAD_FORM_CHANGED"
RESUME_UPLOAD_NOT_CONFIRMED = "RESUME_UPLOAD_NOT_CONFIRMED"
RESUME_UPLOADED = "RESUME_UPLOADED"
_MIME = {".pdf": "application/pdf", ".doc": "application/msword",
         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}

UPLOAD_DESCRIBE_JS = """
() => {
  const visible = e => !!(e && (e.offsetParent !== null || e.getClientRects().length) &&
                          getComputedStyle(e).visibility !== 'hidden');
  document.querySelectorAll('[data-aja-file]').forEach(e => e.removeAttribute('data-aja-file'));
  const panels = Array.from(document.querySelectorAll(
    "[class*='chatbot' i], [role='dialog'], [class*='drawer' i], [class*='modal' i]")).filter(visible);
  const roots = panels.filter(c => !panels.some(o => o !== c && o.contains(c)))
    .filter(r => r.querySelector("input[type='file']"));
  if (roots.length !== 1) return { panels: roots.length, inputs: [] };
  const root = roots[0];
  const inputs = Array.from(root.querySelectorAll("input[type='file']")).map((el, i) => {
    el.setAttribute('data-aja-file', String(i));
    let context = (el.name || '') + ' ' + (el.id || '') + ' ' + (el.getAttribute('aria-label') || '');
    for (let p = el.parentElement, d = 0; p && d < 4 && p !== root; p = p.parentElement, d++) context += ' ' + (p.innerText || '');
    return { ref: String(i), disabled: !!el.disabled, accept: el.getAttribute('accept') || '',
             in_visible_area: visible(el) || visible(el.parentElement),
             resume_context: /resume|\\bcv\\b|curriculum/i.test(context) };
  });
  return { panels: 1, inputs: inputs };
}
"""

UPLOAD_CONFIRM_JS = """
([ref, name]) => {
  const input = document.querySelector(`[data-aja-file="${ref}"]`);
  const root = input ? (input.closest("[class*='chatbot' i], [role='dialog'], [class*='drawer' i], [class*='modal' i]") || document.body) : document.body;
  const text = (root.innerText || '').toLowerCase();
  return { name_seen: text.includes(name.toLowerCase()),
           success_text: /uploaded successfully|upload(ed)? complete|resume uploaded|successfully uploaded/.test(text) };
}
"""


class UploadError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _accepts(accept: str, suffix: str) -> bool:
    parts = [p.strip().lower() for p in (accept or "").split(",") if p.strip()]
    if not parts:
        return True
    mime = _MIME.get(suffix, "")
    return suffix in parts or mime in parts or any(p.endswith("/*") and mime.startswith(p[:-1]) for p in parts)


def choose_upload_control(inputs: list[dict], suffix: str) -> tuple[Optional[str], str]:
    """The single usable file input for this resume type, or (None, reason). Same rules as the Apply
    resolver: usable = enabled, in a visible part of the panel, accepts the file type; several usable
    -> only the single one labelled resume/CV; otherwise fail closed."""
    usable = [i for i in inputs if not i.get("disabled") and i.get("in_visible_area") and _accepts(i.get("accept", ""), suffix)]
    if len(usable) == 1:
        return usable[0]["ref"], ""
    if not usable:
        return None, f"no usable {suffix} upload control among {len(inputs)} file input(s)"
    labelled = [i for i in usable if i.get("resume_context")]
    if len(labelled) == 1:
        return labelled[0]["ref"], ""
    return None, f"{len(usable)} usable upload controls and no single resume upload"


def upload_file_name(markdown_path: Path, suffix: str) -> str:
    """Recruiter-facing name ('<Name>_Resume.pdf' from the resume's first heading), never 'v1.pdf'."""
    try:
        heading = next((l[2:].strip() for l in Path(markdown_path).read_text(encoding="utf-8").splitlines()
                        if l.startswith("# ")), "")
    except OSError:
        heading = ""
    stem = re.sub(r"[^A-Za-z0-9]+", "_", heading).strip("_")[:60]
    return f"{stem + '_' if stem else ''}Resume{suffix}"


@dataclass
class ChatQuestion:
    text: str
    kind: str  # "text" (input/contenteditable), "radio", or "file" (resume upload)
    options: list[str] = field(default_factory=list)
    input_ref: Optional[str] = None
    option_refs: dict = field(default_factory=dict)
    save_ref: Optional[str] = None
    skip_available: bool = False


@dataclass
class ChatState:
    kind: str  # question | error | applied | gone | login | blocked
    question: Optional[ChatQuestion] = None
    code: Optional[str] = None
    reason: str = ""
    signature: str = ""
    text: str = ""  # question text when known (also for errors)


def parse_chat(desc: dict) -> ChatState:
    """Turn a described chat panel into one answerable question, or a fail-closed error."""
    if not desc or not desc.get("present"):
        return ChatState("gone", signature="gone")
    if desc.get("roots", 1) != 1:
        return ChatState("error", code=QUESTION_FORM_CHANGED, reason="more than one chat panel visible", signature="roots")
    text = desc.get("question") or ""
    signature = f"{desc.get('message_count', 0)}|{text}"
    error = lambda code, reason: ChatState("error", code=code, reason=reason, signature=signature, text=text)  # noqa: E731
    if not text:
        return error(QUESTION_FORM_CHANGED, "no question text found in the chat panel")
    inputs, options, saves = desc.get("inputs") or [], desc.get("options") or [], desc.get("saves") or []
    if desc.get("checkbox_count"):
        return error(QUESTION_FORM_CHANGED, "multiple-choice checkboxes are answered by you")
    if desc.get("file_count") and not inputs and not options:  # Naukri asks for a resume file
        if len(saves) > 1:
            return error(QUESTION_FORM_CHANGED, f"{len(saves)} Save buttons shown")
        return ChatState("question", ChatQuestion(text, "file", save_ref=saves[0]["ref"] if saves else None,
                                                  skip_available=bool(desc.get("skip_count"))),
                         signature=signature, text=text)
    if inputs and options:
        return error(QUESTION_FORM_CHANGED, "both a text box and choices are shown")
    if len(inputs) > 1:
        return error(QUESTION_FORM_CHANGED, f"{len(inputs)} answer boxes shown")
    if not inputs and not options:
        return error(QUESTION_INPUT_NOT_FOUND, "no answer box or choices found")
    if not saves:
        return error(QUESTION_SAVE_NOT_FOUND, "no Save button found")
    if len(saves) > 1:
        return error(QUESTION_FORM_CHANGED, f"{len(saves)} Save buttons shown")
    if options and len({o["text"].lower() for o in options}) != len(options):
        return error(QUESTION_FORM_CHANGED, "duplicate choices shown")
    question = ChatQuestion(
        text=text,
        kind="radio" if options else ("textarea" if inputs[0]["kind"] == "textarea" else "text"),
        options=[o["text"] for o in options],
        input_ref=inputs[0]["ref"] if inputs else None,
        option_refs={o["text"]: o["ref"] for o in options},
        save_ref=saves[0]["ref"],
        skip_available=bool(desc.get("skip_count")),
    )
    return ChatState("question", question, signature=signature, text=text)


def validate_resume_file(path: Optional[Path]) -> tuple[bool, str]:
    if path is None:
        return False, "no resume path"
    path = Path(path)
    if not path.exists():
        return False, f"resume file not found: {path}"
    if not path.is_file():
        return False, f"resume path is not a file: {path}"
    if path.suffix.lower() not in REVIEWABLE_RESUME_TYPES:
        return False, f"unsupported resume type {path.suffix or '(none)'}"
    try:
        with path.open("rb") as handle:
            if not handle.read(1):
                return False, f"resume file is empty: {path}"
    except OSError as e:
        return False, f"resume file not readable: {e}"
    return True, ""


def resolve_resume(job_url: str, agent_output_dir: Optional[Path]
                   ) -> tuple[Optional[ResumeInfo], Optional[str], str]:
    """(ResumeInfo, None, "") for this job's verified AI Agent resume, else (None, code, reason).
    Discovery and validation live in resume_artifact.resolve_verified_resume (one place)."""
    artifact, code, reason = resolve_verified_resume(job_url, agent_output_dir)
    if artifact is None:
        return None, code, reason
    path = artifact.upload_path or artifact.markdown_path
    return ResumeInfo(path, artifact.uploadable, artifact.drive_link, artifact.resume_id, artifact.version,
                      artifact.markdown_path, artifact.upload_code, artifact.upload_reason), None, ""


def find_tailored_resume(job_url: str, agent_output_dir: Optional[Path]) -> tuple[Optional[ResumeInfo], str]:
    """Compatibility wrapper: (ResumeInfo, "") or (None, reason)."""
    resume, _, reason = resolve_resume(job_url, agent_output_dir)
    return resume, reason


# ---------------------------------------------------------------------------
# Browser layer (headed, saved profile, read-mostly)
# ---------------------------------------------------------------------------

SNAPSHOT_JS = """
() => {
  const visible = e => !!(e && (e.offsetParent !== null || e.getClientRects().length));
  const count = sel => Array.from(document.querySelectorAll(sel)).filter(visible).length;
  // Naukri renders its Applied state as <span id="already-applied">, not a button: include it by id/class.
  const buttons = Array.from(document.querySelectorAll("button, a, #already-applied, [class*='already-applied' i]"))
    .filter(b => visible(b) && (/apply/i.test(b.id || '') ||
      /^\\s*(apply|easy apply|apply now|applied|already applied|apply on company site|login to apply|register to apply)\\s*$/i
        .test(b.innerText || '')))
    .map(b => ({ id: b.id || '', text: (b.innerText || '').trim().slice(0, 60),
                 href: b.getAttribute('href') || '', disabled: !!b.disabled || b.getAttribute('aria-disabled') === 'true',
                 in_job_header: !!b.closest('#job_header') }));
  return {
    url: location.href,
    title: document.title,
    body: document.body ? document.body.innerText.slice(0, 4000) : '',
    has_captcha_frame: !!document.querySelector("iframe[src*='captcha'], iframe[src*='recaptcha']"),
    login_links: count('#login_Layer, #register_Layer'),
    logged_in_markers: count(".nI-gNb-drawer, .nI-gNb-drawer__icon, .nI-gNb-info, a[href*='mnjuser']"),
    has_job_header: !!document.querySelector("[class*='jd-header-title']"),
    buttons: buttons,
    // labels only (Naukri UI text, no page content) of the job header's visible controls, for diagnostics
    header_controls: Array.from(document.querySelectorAll('#job_header button, #job_header a[class*="button" i]'))
      .filter(visible).map(b => (b.innerText || '').trim().slice(0, 40)).filter(Boolean).slice(0, 12),
  };
}
"""

FORM_JS = """
() => {
  const visible = e => !!(e && (e.offsetParent !== null || e.getClientRects().length));
  const container = Array.from(document.querySelectorAll("[role='dialog'], [class*='drawer' i], [class*='modal' i]"))
    .filter(visible).find(c => c.querySelector('input, select, textarea'));
  const chatbot = Array.from(document.querySelectorAll("[class*='chatbot' i]")).some(visible);
  if (!container) return { chatbot: chatbot, form: false, questions: [] };
  const labelFor = el => {
    if (el.id) { const l = container.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText; }
    const wrap = el.closest('label'); if (wrap) return wrap.innerText;
    return el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '';
  };
  const questions = []; const seenRadio = new Set();
  Array.from(container.querySelectorAll('input, select, textarea')).filter(visible).forEach((el, i) => {
    const type = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text');
    if (['hidden', 'submit', 'button'].includes(type)) return;
    if (type === 'radio') {
      if (seenRadio.has(el.name)) return; seenRadio.add(el.name);
      const group = Array.from(container.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`));
      const legend = el.closest('fieldset')?.querySelector('legend')?.innerText || el.name;
      questions.push({ index: i, type: 'radio', name: el.name, label: legend.trim(), options: group.map(r => labelFor(r).trim()) });
      return;
    }
    questions.push({ index: i, type: type, label: labelFor(el).trim(),
                     options: type === 'select' ? Array.from(el.options).map(o => o.text.trim()) : [] });
  });
  return { chatbot: chatbot, form: true, questions: questions };
}
"""


class NaukriApplicationBrowser:
    """Headed Playwright session on the saved chrome_user_data/ profile (shared with the scanner)."""

    def __init__(self, timeout_seconds: int = NAUKRI_APPLICATION_TIMEOUT):
        self.timeout_ms = timeout_seconds * 1000
        self._playwright = self._context = self.page = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> None:
        if self.page:
            return
        from playwright.sync_api import sync_playwright

        from naukri_scanner import launch_browser_context

        self._playwright = sync_playwright().start()
        try:
            self._context = launch_browser_context(self._playwright, headless=False)
        except Exception as e:
            self.close()
            text = str(e).lower()
            if "processsingleton" in text or "already in use" in text or "lock" in text:
                raise ApplicationStop(BROWSER_PROFILE_LOCKED, "The chrome_user_data/ profile is open in another "
                                      "browser window or process. Close it and run again (the profile is never reset).")
            raise
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def close(self) -> None:
        for resource, action in ((self._context, "close"), (self._playwright, "stop")):
            if resource:
                try:
                    getattr(resource, action)()
                except Exception as e:  # cleanup must not hide the real error
                    logger.debug(f"browser cleanup: {e}")
        self._playwright = self._context = self.page = None

    def snapshot(self) -> dict:
        return self.page.evaluate(SNAPSHOT_JS)

    def open_job(self, url: str) -> dict:
        self.start()
        self.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        try:
            self.page.wait_for_selector("[class*='jd-header-title'], #login_Layer, .nI-gNb-drawer",
                                        timeout=self.timeout_ms, state="attached")
        except Exception:
            logger.warning(f"Job page content did not appear within the timeout: {url}")
        self.page.wait_for_timeout(2500)  # header/apply widgets render after the main content
        return self.snapshot()

    def describe_apply_controls(self) -> list[dict]:
        """Safe facts about every #apply-button (no page content beyond the control itself)."""
        buttons = self.page.locator(APPLY_CONTROL_SELECTOR)
        described = []
        for index in range(buttons.count()):
            button = buttons.nth(index)
            context = button.evaluate(APPLY_CONTEXT_JS)
            described.append({
                "index": index,
                "visible": button.is_visible(),
                "enabled": button.is_enabled(),
                "text": (button.inner_text() or "").strip(),
                **context,
            })
        return described

    def resolve_apply_control(self):
        """The single actionable Naukri Apply locator, or RuntimeError (APPLICATION_FORM_CHANGED)."""
        candidates = self.describe_apply_controls()
        index, reason = choose_apply_control(candidates)
        if len(candidates) > 1 or index is None:
            logger.info("Apply controls: " + "; ".join(
                f"[{c['index']}] visible={c['visible']} enabled={c['enabled']} text={c['text']!r} "
                f"job_header={c['in_job_header']} sticky={c['in_sticky']}" for c in candidates))
        if index is None:
            raise RuntimeError(reason)
        return self.page.locator(APPLY_CONTROL_SELECTOR).nth(index), len(candidates)

    def click_easy_apply(self) -> None:
        """Click Naukri's own Apply control, chosen by choose_apply_control() and re-verified first."""
        button, expected_count = self.resolve_apply_control()
        if self.page.locator(APPLY_CONTROL_SELECTOR).count() != expected_count:
            raise RuntimeError("apply controls changed while resolving")
        text = (button.inner_text() or "").strip().lower()
        if text not in APPLY_TEXTS or not button.is_visible() or not button.is_enabled():
            raise RuntimeError(f"apply control not in the expected state (text={text!r})")
        if not button.evaluate("el => el.isConnected"):
            raise RuntimeError("apply control detached from the page")
        # From here on the click may have happened: failures are uncertain, never "not clicked".
        try:
            button.click(timeout=self.timeout_ms)
        except Exception as e:
            raise ApplyClickUncertain(f"Apply click did not complete cleanly ({type(e).__name__})") from e

    def await_confirmation(self, seconds: int, job_url: Optional[str] = None) -> ConfirmationResult:
        """Wait for a delayed confirmation, then re-open the job page (read-only) and check once more."""
        for _ in range(max(0, seconds)):
            result = detect_application_confirmation(self.snapshot())
            if result.confirmed or result.failed:
                return result
            self.page.wait_for_timeout(1000)
        result = detect_application_confirmation(self.snapshot())
        if not result.confirmed and job_url:
            result = detect_application_confirmation(self.open_job(job_url))
        return result

    def describe_structure(self) -> dict:
        """Counts only (no text) of the visible application UI, for diagnostics."""
        chat = self.page.evaluate(CHAT_DESCRIBE_JS)
        form = self.page.evaluate(FORM_JS)
        return {
            "chat_present": bool(chat.get("present")), "chat_panels": chat.get("roots"),
            "chat_inputs": len(chat.get("inputs") or []), "chat_options": len(chat.get("options") or []),
            "chat_saves": len(chat.get("saves") or []), "file_inputs": chat.get("file_count"),
            "form_present": bool(form.get("form")), "form_questions": len(form.get("questions") or []),
            "apply_controls": self.page.locator(APPLY_CONTROL_SELECTOR).count(),
        }

    def wait_for_outcome(self) -> dict:
        """Poll after Apply: success, form/chatbot questions, login, block, failure, or unknown."""
        pages_before = len(self._context.pages)
        deadline = self.timeout_ms
        while deadline > 0:
            self.page.wait_for_timeout(1000)
            deadline -= 1000
            if len(self._context.pages) > pages_before:
                return {"kind": "new_tab", "url": self._context.pages[-1].url}
            snap = self.snapshot()
            body = snap["body"].lower()
            if is_login_page(snap["url"]):
                return {"kind": "login"}
            if snap.get("has_captcha_frame") or detect_block(snap["title"], snap["url"], body):
                return {"kind": "blocked"}
            confirmation = detect_application_confirmation(snap)
            if confirmation.confirmed:
                return {"kind": "applied", "signal": confirmation.signal}
            if confirmation.failed:
                return {"kind": "failed", "reason": confirmation.reason}
            form = self.page.evaluate(FORM_JS)
            if form["chatbot"]:
                return {"kind": "chatbot"}
            if form["form"]:
                return {"kind": "form", "questions": form["questions"]}
        return {"kind": "unknown"}

    def chat_state(self) -> ChatState:
        """Current recruiter-question state: applied / login / blocked / question / error / gone."""
        snap = self.snapshot()
        body = snap["body"].lower()
        if is_login_page(snap["url"]):
            return ChatState("login", signature="login")
        if snap.get("has_captcha_frame") or detect_block(snap["title"], snap["url"], body):
            return ChatState("blocked", code=CAPTCHA_REQUIRED, reason="Naukri verification page", signature="blocked")
        confirmation = detect_application_confirmation(snap)
        if confirmation.confirmed:
            return ChatState("applied", reason=confirmation.signal, signature="applied")
        desc = self.page.evaluate(CHAT_DESCRIBE_JS)
        state = parse_chat(desc)
        if state.kind == "error":  # structure only, never answers or page text beyond the question
            logger.info(f"Chat panel not answerable ({state.code}): inputs={[i['kind'] for i in desc.get('inputs') or []]} "
                        f"options={len(desc.get('options') or [])} saves={[s['text'] for s in desc.get('saves') or []]} "
                        f"checkboxes={desc.get('checkbox_count')} roots={desc.get('roots')}")
        return state

    def _marked(self, ref: str):
        target = self.page.locator(f"[data-aja='{ref}']")
        if target.count() != 1 or not target.nth(0).is_visible():
            raise RuntimeError(f"question element {ref} changed before it could be used")
        return target.nth(0)

    def answer_chat(self, question: ChatQuestion, value: str) -> None:
        """Enter one answer exactly as given and click the question's own Save."""
        if question.kind == "radio":
            ref = question.option_refs.get(value)
            if ref is None:
                raise RuntimeError("answer is not one of the offered choices")
            self._marked(ref).click(timeout=self.timeout_ms)
        else:
            box = self._marked(question.input_ref)
            box.click(timeout=self.timeout_ms)
            box.fill(value, timeout=self.timeout_ms)
        save = self._marked(question.save_ref)
        if not save.is_enabled():
            raise RuntimeError("Save is not enabled")
        save.click(timeout=self.timeout_ms)

    def wait_for_chat_change(self, signature: str) -> ChatState:
        """Wait until the current question is replaced (next question, confirmation, ...)."""
        state = self.chat_state()
        waited = 0
        while state.signature == signature and waited < self.timeout_ms:
            self.page.wait_for_timeout(1000)
            waited += 1000
            state = self.chat_state()
        return state

    def fill_form(self, planned: list[tuple[dict, str]], resume: Optional[ResumeInfo]) -> None:
        container = self.page.locator("[role='dialog'], [class*='drawer' i], [class*='modal' i]").filter(
            has=self.page.locator("input, select, textarea")).first
        fields = container.locator("input, select, textarea")
        for question, value in planned:
            if question["type"] == "radio":
                container.get_by_label(value, exact=True).check()
            elif question["type"] == "select":
                fields.nth(question["index"]).select_option(label=value)
            else:
                fields.nth(question["index"]).fill(value)
        # Resume files are uploaded separately through upload_resume() (resolver + confirmation).

    def upload_resume(self, resume: "ResumeInfo") -> None:
        """Upload the validated job-specific file into the one resolved upload control, then confirm
        Naukri shows it. Raises UploadError(RESUME_UPLOAD_FORM_CHANGED / RESUME_UPLOAD_NOT_CONFIRMED)."""
        import shutil
        import tempfile

        if not resume.uploadable:
            raise UploadError(resume.upload_code or "RESUME_PDF_NOT_FOUND", resume.upload_reason or "no uploadable file")
        desc = self.page.evaluate(UPLOAD_DESCRIBE_JS)
        if desc.get("panels") != 1:
            raise UploadError(RESUME_UPLOAD_FORM_CHANGED, f"{desc.get('panels')} application panels with a file upload")
        ref, why = choose_upload_control(desc["inputs"], resume.path.suffix.lower())
        if len(desc["inputs"]) > 1 or ref is None:
            logger.info("Upload controls: " + "; ".join(
                f"[{i['ref']}] disabled={i['disabled']} visible_area={i['in_visible_area']} accept={i['accept']!r} "
                f"resume_label={i['resume_context']}" for i in desc["inputs"]))
        if ref is None:
            raise UploadError(RESUME_UPLOAD_FORM_CHANGED, why)
        control = self.page.locator(f"[data-aja-file='{ref}']")
        if control.count() != 1:
            raise UploadError(RESUME_UPLOAD_FORM_CHANGED, "upload control changed before uploading")
        name = upload_file_name(resume.markdown_path or resume.path, resume.path.suffix.lower())
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / name
            shutil.copyfile(resume.path, named)
            control.nth(0).set_input_files(str(named), timeout=self.timeout_ms)
        for _ in range(max(1, self.timeout_ms // 1000)):
            seen = self.page.evaluate(UPLOAD_CONFIRM_JS, [ref, name])
            if seen["name_seen"] or seen["success_text"]:
                return
            self.page.wait_for_timeout(1000)
        raise UploadError(RESUME_UPLOAD_NOT_CONFIRMED, "Naukri did not show the uploaded file")

    def click_chat_save(self, question: "ChatQuestion") -> None:
        if question.save_ref:
            self._marked(question.save_ref).click(timeout=self.timeout_ms)

    def submit_form(self) -> None:
        container = self.page.locator("[role='dialog'], [class*='drawer' i], [class*='modal' i]").filter(
            has=self.page.locator("input, select, textarea")).first
        submit = container.locator("button").filter(has_text=re.compile(r"^\s*(submit|apply|save)\s*$", re.I))
        if submit.count() != 1:
            raise RuntimeError(f"expected exactly one submit button in the application form, found {submit.count()}")
        submit.first.click()

    def session_state(self) -> str:
        """Phase 4: read-only login check on the saved profile ('authenticated', 'login_required' or
        'unknown'). Opens Naukri's homepage; never types, clicks or submits anything."""
        self.start()
        self.page.goto("https://www.naukri.com/mnjuser/homepage", wait_until="domcontentloaded", timeout=self.timeout_ms)
        self.page.wait_for_timeout(2500)  # the header renders after the main content
        return classify_auth(self.snapshot())

    def wait_for_login(self, minutes: int, out: Callable[[str], None] = print) -> bool:
        """Open Naukri's login page and wait while the user logs in by hand. Never types anything."""
        self.start()
        self.page.goto("https://www.naukri.com/nlogin/login", wait_until="domcontentloaded", timeout=self.timeout_ms)
        out(f"Log in to Naukri in the opened browser window (you have {minutes} minutes). "
            "Complete any OTP/CAPTCHA yourself. This tool never sees your password.")
        for _ in range(minutes * 12):
            self.page.wait_for_timeout(5000)
            if "naukri.com" in self.page.url and not is_login_page(self.page.url):
                try:
                    if classify_auth(self.snapshot()) == "authenticated":
                        return True
                except Exception:
                    continue
        return False


# ---------------------------------------------------------------------------
# Runner: review gate, submission, recording
# ---------------------------------------------------------------------------


def resume_lines(resume: ResumeInfo) -> list[str]:
    """Artifact status for review/dry-run output (paths and flags only, never resume content)."""
    lines = [f"Verified resume: YES (AI Agent v{resume.version or '?'})",
             f"Markdown: {resume.markdown_path or resume.path}"]
    if resume.uploadable:
        lines += [f"PDF available: {'YES' if resume.path.suffix.lower() == '.pdf' else 'NO (DOCX used)'}",
                  f"Upload candidate: {resume.path}"]
    else:
        lines += [f"PDF available: NO ({resume.upload_code}: {resume.upload_reason})",
                  "Upload candidate: none - if Naukri asks for a file, you will be asked to upload it yourself"]
    return lines


def format_review(job: dict, resume: Optional[ResumeInfo], assessment: Assessment) -> str:
    line, thin = "=" * 50, "-" * 50
    parts = [line, "APPLICATION REVIEW", line, "",
             f"Job: {job['title']}", f"Company: {job['company']}", f"Location: {job.get('location') or '-'}",
             f"Match Score: {int(job.get('match_score') or 0)}/100", f"Status: {job.get('match_status')}", "",
             "Matched Skills:", *[f"- {s}" for s in job.get("matching_skills") or ["(none listed)"]], "",
             "Missing Skills:", *[f"- {s}" for s in job.get("missing_skills") or ["(none listed)"]], ""]
    if job.get("match_reason"):
        parts += ["AI Agent reasoning:", job["match_reason"], ""]
    if resume:
        parts += resume_lines(resume)
        if resume.drive_link:
            parts += [f"Tailored resume in Drive: {resume.drive_link}"]
        parts += ["Note: Naukri's Apply sends the resume on your Naukri profile; the tailored file is",
                  "uploaded only if the application form asks for a file.", ""]
    parts += [f"Application URL:", job["url"], "", f"Current Application State: {assessment.reason or assessment.state}",
              "", thin, "Approving clicks Naukri's Apply button. Naukri may submit immediately.",
              "Approve this application?", "[y] Yes", "[n] No", "[s] Skip", "[q] Quit", thin]
    return "\n".join(parts)


class ApplicationRunner:
    def __init__(
        self,
        browser,
        prompt: Callable[[str], str] = input,
        out: Callable[[str], None] = print,
        dry_run: bool = False,
        answers=None,
        agent_output_dir: Optional[Path] = AI_AGENT_OUTPUT_DIR,
        evidence: Optional[ResumeEvidence] = None,
    ):
        """answers: ApplicationProfile or a field->value mapping (default: APPLICANT_* from .env).
        evidence: master-resume skills for yes/no skill questions (default: MASTER_RESUME_PATH)."""
        self.browser, self.prompt, self.out, self.dry_run = browser, prompt, out, dry_run
        self.profile = ApplicationProfile.from_environment() if answers is None else _as_profile(answers)
        self.evidence = ResumeEvidence.from_file() if evidence is None else evidence
        self.agent_output_dir = agent_output_dir
        self.navigation_retries = NAUKRI_NAVIGATION_RETRIES
        self.confirmation_wait = CONFIRMATION_WAIT_SECONDS
        self.debug_dir: Optional[Path] = None  # None: APPLICATION_DEBUG_DIR
        self._attempt_id: Optional[int] = None  # the open application_attempts row (non-dry runs)
        self._clicked = False  # True from just before the Apply click: the outcome may be a submission

    # -- attempt lifecycle, events, diagnostics ------------------------------------------

    def _secrets(self) -> list[str]:
        return [getattr(self.profile, name) for name in self.profile.configured()]

    def _event(self, event: str, job: dict, state: Optional[str] = None, **fields) -> None:
        log_event(event, job, state, self._secrets(), **fields)

    def _begin(self) -> None:
        self._attempt_id, self._clicked = None, False

    def _mark_clicked(self, clicked: bool) -> None:
        self._clicked = clicked
        if self._attempt_id is not None:
            mark_apply_clicked(self._attempt_id, clicked)

    def _diagnose(self, job: dict, state: str, code: Optional[str], reason: str, resume) -> Optional[Path]:
        """Sanitized failure record. A failure to capture it is logged, never raised over the real result."""
        snapshot = structure = None
        try:
            if getattr(self.browser, "page", True) is not None:  # a real browser that never opened has page=None
                snapshot = self.browser.snapshot()
                describe = getattr(self.browser, "describe_structure", None)
                structure = describe() if describe else None
        except Exception as e:  # the page may be gone (that can be the failure itself)
            logger.warning(f"Diagnostics: page not readable ({type(e).__name__})")
        resume_meta = {"resume_id": resume.resume_id, "version": resume.version, "file": str(resume.path),
                       "uploadable": resume.uploadable} if resume else {}
        record = build_diagnostics(job, state, code, reason, snapshot, structure, resume_meta, self._secrets())
        try:
            return write_diagnostics(job, record, self.debug_dir)
        except OSError as e:
            logger.warning(f"Diagnostics not written: {e}")
            return None

    # -- helpers -------------------------------------------------------------

    def _ask(self, message: str, choices: str) -> str:
        """Exact answer only ('y'/'yes', 'n'/'no', 's'/'skip', 'q'/'quit'); EOF, interrupts and anything
        else never count as approval."""
        try:
            answer = (self.prompt(message) or "").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q"
        if not answer:
            return ""  # plain Enter: never approval
        answer = {"yes": "y", "no": "n", "skip": "s", "quit": "q"}.get(answer, answer)
        return answer if len(answer) == 1 and answer in choices else "n"

    def _set(self, job: dict, state: str, code: Optional[str] = None, reason: str = "",
             external_url: Optional[str] = None) -> None:
        if not self.dry_run:
            set_application_state(job["id"], state, code, reason, external_url)
        logger.info(f"Application state | db_id={job['id']} | naukri_id={job.get('job_id')} | "
                    f"{job['title']} @ {job['company']} | state={state} | code={code} | {reason}")

    _FINAL_AFTER_CLICK = ("applied", "already_applied", "external_application", "recovery_required")
    _NO_DIAGNOSTICS = ("applied", "already_applied", "external_application", "job_unavailable", "ready")

    def _result(self, job: dict, state: str, code: Optional[str] = None, reason: str = "", **extra) -> ApplicationResult:
        """Every outcome goes through here. Once Apply may have been clicked, anything but a confirmed,
        external or already-applied outcome becomes recovery_required: it is never retried blindly."""
        if self._clicked and state not in self._FINAL_AFTER_CLICK:
            reason = f"{reason} [Apply was clicked; the outcome needs recovery]".strip()
            state = "recovery_required"
        self._set(job, state, code, reason, extra.get("external_url"))
        result = ApplicationResult(job["id"], state, code, reason, **extra)
        if self.dry_run:
            return result
        if self._attempt_id is not None:
            finish_attempt(self._attempt_id, state, code, reason, success=state == "applied",
                           confirmation_signal=result.confirmation_signal, external_url=result.external_url)
            self._attempt_id = None
        if state == "applied":
            self._event("APPLICATION_CONFIRMED", job, state, signal=result.confirmation_signal)
        elif state == "recovery_required":
            self._event("RECOVERY_REQUIRED", job, state, code=code)
        elif state not in self._NO_DIAGNOSTICS:
            self._event("APPLICATION_FAILED", job, state, code=code)
        if state not in self._NO_DIAGNOSTICS and code != NOT_APPROVED:
            result.diagnostics_path = self._diagnose(job, state, code, reason, result.resume)
        return result

    def _record(self, job: dict, status: str, resume: Optional[ResumeInfo], reason: Optional[str] = None,
                external_url: Optional[str] = None) -> None:
        if self.dry_run:  # never reachable in practice: dry runs stop before any click
            return
        record_application_attempt(job, status, str(resume.path) if resume else None, job["url"], reason, external_url)

    # -- verification ----------------------------------------------------------

    def verify(self, job: dict) -> Assessment:
        """Open the job and assess it. Raises ApplicationStop when login/CAPTCHA blocks everything.
        Page loads are retried (NAUKRI_NAVIGATION_RETRIES) -- this is before Apply, so it is safe."""
        for attempt in range(self.navigation_retries + 1):
            try:
                snapshot = self.browser.open_job(job["url"])
                break
            except ApplicationStop:
                raise
            except Exception as e:
                if attempt == self.navigation_retries:
                    raise
                logger.warning(f"Job page load failed ({type(e).__name__}); retry {attempt + 1}/{self.navigation_retries}")
        self._event("JOB_OPENED", job)
        assessment = assess_job_page(snapshot, job["url"])
        self._event("JOB_STATE_CHECKED", job, assessment.state, code=assessment.code)
        if assessment.code == CAPTCHA_REQUIRED and not self.dry_run:
            self.out("CAPTCHA/manual verification required.\nComplete the verification manually in the browser.\n"
                     "Then resume the application workflow.")
            if self._ask("Press Enter when done, or q to stop: ", "q") == "q":
                raise ApplicationStop(CAPTCHA_REQUIRED, assessment.reason)
            assessment = assess_job_page(self.browser.snapshot(), job["url"])
        return assessment

    # -- one job ------------------------------------------------------------------

    def idempotency(self, job: dict):
        """Decide whether a new application may be prepared; closes stale attempts (non-dry runs)."""
        decision = decide_idempotency(job, has_successful_application(job), get_application_record(job),
                                      open_attempts(job["id"]))
        if not self.dry_run:
            for attempt_id, state, code in decision.close_open_attempts:
                finish_attempt(attempt_id, state, code, "previous run ended without finishing this attempt")
        return decision

    def process(self, job: dict) -> ApplicationResult:
        self._begin()
        self._event("APPLICATION_START", job, dry_run=self.dry_run or None)
        decision = self.idempotency(job)
        if not decision.proceed:
            self._event("IDEMPOTENCY_BLOCKED", job, decision.state, code=decision.code)
            if decision.state == "already_applied":
                return self._result(job, "already_applied", ALREADY_APPLIED, "already recorded as applied in applied_jobs")
            if self.dry_run:
                self.out(f"DRY RUN: blocked by idempotency check: {decision.reason}. "
                         f"Recover first: python main.py --action recover-application --job-id {job['id']}")
            return self._result(job, "recovery_required", decision.code,
                                f"{decision.reason}; run: python main.py --action recover-application --job-id {job['id']}")
        self._set(job, "preparing")
        resume, code, why = resolve_resume(job["url"], self.agent_output_dir)
        if resume is not None:
            ok, file_why = validate_resume_file(resume.path)
            if not ok:
                resume, code, why = None, RESUME_NOT_FOUND, file_why
            elif resume.upload_code == RESUME_ARTIFACT_MISMATCH:  # a file made for another job: stop
                resume, code, why = None, RESUME_ARTIFACT_MISMATCH, resume.upload_reason
        if resume is None:
            return self._result(job, "requires_manual_action", code or RESUME_NOT_FOUND, why)

        try:
            assessment = self.verify(job)
        except ApplicationStop as stop:  # CAPTCHA not cleared, profile locked, ...: record the real reason
            self._set(job, "requires_manual_action", stop.code, str(stop))
            raise
        except Exception as e:
            return self._result(job, "requires_manual_action", NETWORK_TIMEOUT, f"could not load the job page: {e}")

        if assessment.state == "requires_login":
            self._set(job, "requires_login", AUTH_REQUIRED, assessment.reason)
            raise ApplicationStop(AUTH_REQUIRED, "Naukri login required. Run `python main.py --action naukri-login`, "
                                  "log in by hand in the opened browser, then run this command again.")
        if assessment.state == "already_applied":
            record = get_application_record(job)
            if record and record.get("status") != "applied" and not self.dry_run:
                record_application_attempt(job, "applied", record.get("resume_path"), job["url"],
                                           "confirmed applied on Naukri after an earlier unconfirmed attempt")
            return self._result(job, "already_applied", ALREADY_APPLIED, assessment.reason, resume=resume)
        if assessment.state != "ready":
            return self._result(job, assessment.state, assessment.code, assessment.reason,
                                external_url=assessment.external_url, resume=resume)

        self.out(format_review(job, resume, assessment))
        self._event("REVIEW_SHOWN", job, "review_required")
        if self.dry_run:
            configured = self.profile.configured()
            self.out("DRY RUN: the job is ready. A real run would ask for approval here and, only after 'y', "
                     "click Naukri's Apply button; a resume file would be uploaded only if Naukri asks for one.\n"
                     f"Current application state: {job.get('application_state') or 'not prepared'}"
                     f" ({job.get('application_code') or '-'})\n"
                     "Idempotency: no successful or unresolved earlier attempt; Naukri does not show Applied\n"
                     "Recovery status: not required\n"
                     + "\n".join(resume_lines(resume))
                     + "\nQuestion handling: answered automatically only from "
                     + (", ".join(configured) if configured else "nothing (no APPLICANT_* configured)")
                     + "; everything else goes to you\n"
                     "Submission requires your explicit 'y' approval: YES\n"
                     "Nothing was clicked, uploaded, answered, submitted or recorded.")
            return ApplicationResult(job["id"], "ready", None, "dry run: ready to apply", resume=resume)

        self._set(job, "review_required", None, "waiting for human approval")
        choice = self._ask("Approve this application? [y/n/s/q]: ", "ynsq")
        if choice == "q":
            self._set(job, "ready", NOT_APPROVED, "run stopped at review")
            raise ApplicationStop(NOT_APPROVED, "Stopped by user at the review step.")
        if choice != "y":
            return self._result(job, "ready", NOT_APPROVED, "not approved at review" if choice == "n" else "skipped at review",
                                resume=resume)
        self._set(job, "approved", None, "approved by user")
        self._event("APPROVAL_RECEIVED", job, "approved")
        self._attempt_id = start_attempt(job, "apply", str(resume.path), resume.version)
        return self._submit(job, resume)

    def _submit(self, job: dict, resume: ResumeInfo) -> ApplicationResult:
        self._set(job, "submitting")
        self._mark_clicked(True)  # written before the click: a crash can never hide it
        try:
            self.browser.click_easy_apply()
        except RuntimeError as e:  # resolver/pre-click checks: Apply was provably not clicked
            self._mark_clicked(False)
            return self._result(job, "failed", APPLICATION_FORM_CHANGED, f"apply control not clickable: {e}", resume=resume)
        except Exception as e:  # ApplyClickUncertain, browser closed, ...: may have submitted
            return self._result(job, "recovery_required", SUBMISSION_UNCONFIRMED,
                                f"Apply click outcome unknown ({type(e).__name__}); submission may have occurred",
                                resume=resume)
        self._event("APPLY_CLICKED", job, "submitting")
        try:
            return self._after_apply(job, resume)
        except ApplicationStop:
            raise
        except Exception as e:  # browser crash / network failure after Apply: never retried
            logger.warning(f"Application flow interrupted after Apply: {type(e).__name__}")
            return self._result(job, "recovery_required", SUBMISSION_UNCONFIRMED,
                                f"browser or network failure after Apply ({type(e).__name__}); "
                                "submission may have occurred; manual verification required", resume=resume)

    def _confirmed(self, job: dict, resume: ResumeInfo, signal: Optional[str], reason: str) -> ApplicationResult:
        self._event("CONFIRMATION_DETECTED", job, "submitting", signal=signal)
        self._record(job, "applied", resume)
        return self._result(job, "applied", APPLIED, reason, submitted=True, resume=resume, confirmation_signal=signal)

    def _unconfirmed(self, job: dict, resume: ResumeInfo, what: str) -> ApplicationResult:
        """Wait for a delayed confirmation; otherwise stop for recovery (never retry)."""
        self._event("SUBMISSION_PENDING", job, "submitting")
        confirmation = self.browser.await_confirmation(self.confirmation_wait, job["url"])
        if confirmation.confirmed:
            return self._confirmed(job, resume, confirmation.signal, f"Naukri confirmed the application ({confirmation.reason})")
        status = "submission_failed" if confirmation.failed else "unconfirmed"
        self._record(job, status, resume, f"{what}: {confirmation.reason}")
        code = SUBMISSION_FAILED if confirmation.failed else SUBMISSION_UNCONFIRMED
        return self._result(job, "recovery_required", code,
                            f"{what}. Submission may have occurred but confirmation could not be verified "
                            f"({confirmation.reason}); manual verification required.", resume=resume)

    def _after_apply(self, job: dict, resume: ResumeInfo) -> ApplicationResult:
        outcome = self.browser.wait_for_outcome()
        kind = outcome["kind"]
        if kind == "applied":
            return self._confirmed(job, resume, outcome.get("signal"), "Naukri confirmed the application")
        if kind == "failed":
            return self._unconfirmed(job, resume, f"Naukri reported a problem after Apply ({outcome.get('reason')})")
        if kind == "login":
            return self._result(job, "requires_login", AUTH_REQUIRED, "Naukri asked for login after Apply", resume=resume)
        if kind == "new_tab":
            self._record(job, "external_application", resume, "Apply opened an external site", outcome.get("url"))
            return self._result(job, "external_application", EXTERNAL_APPLICATION, "Apply opened an external site",
                                external_url=outcome.get("url"), resume=resume)
        if kind == "form":
            return self._handle_form(job, resume, outcome["questions"])
        if kind == "chatbot":
            return self._handle_chat(job, resume)
        if kind == "blocked":
            return self._manual_handoff(job, resume, CAPTCHA_REQUIRED, "Naukri showed a verification page")
        return self._unconfirmed(job, resume, "Apply was clicked but no confirmation or question panel appeared")

    def _handle_form(self, job: dict, resume: ResumeInfo, form_questions: list[dict]) -> ApplicationResult:
        planned, unsupported = plan_answers(form_questions, self.profile, self.evidence)
        wants_file = any(q.get("type") == "file" for q in form_questions)
        if wants_file and resume.uploadable:
            unsupported = [(q, r) for q, r in unsupported if q.get("type") != "file"]
        elif wants_file:
            unsupported = [(q, f"{resume.upload_code}: {resume.upload_reason}" if q.get("type") == "file" else r)
                           for q, r in unsupported]
        if unsupported:
            details = "; ".join(f"{q.get('label') or q.get('type')}: {r}" for q, r in unsupported)
            return self._manual_handoff(job, resume, UNSUPPORTED_QUESTION, f"questions need you: {details}")
        try:
            self.browser.fill_form(planned, resume)
            if wants_file:
                self.browser.upload_resume(resume)
                logger.info(f"Resume uploaded | db_id={job['id']} | resume_id={resume.resume_id} | v{resume.version} | "
                            f"{resume.path.suffix[1:]}")
        except UploadError as e:
            return self._manual_handoff(job, resume, e.code, f"resume upload: {e}")
        except Exception as e:
            return self._manual_handoff(job, resume, APPLICATION_FORM_CHANGED, f"could not fill the form: {e}")
        self.out("Filled from your configured facts:\n" + "\n".join(f"- {q['label']}: {v}" for q, v in planned))
        if self._ask("Submit these answers? [y/n]: ", "yn") != "y":
            self._record(job, "incomplete", resume, "answers not approved; form left open")
            return self._result(job, "requires_manual_action", NOT_APPROVED, "answers not approved", resume=resume)
        try:
            self.browser.submit_form()
        except Exception as e:
            return self._manual_handoff(job, resume, APPLICATION_FORM_CHANGED, f"could not submit the form: {e}")
        outcome = self.browser.wait_for_outcome()
        if outcome["kind"] == "applied":
            return self._confirmed(job, resume, outcome.get("signal"), "Naukri confirmed the application")
        return self._unconfirmed(job, resume, "the form was submitted but no confirmation appeared")

    def _handle_chat(self, job: dict, resume: ResumeInfo) -> ApplicationResult:
        """Answer Naukri's recruiter questions one by one: configured facts automatically, everything
        else by the human. Applied only when Naukri confirms; 'Save' alone proves nothing."""
        self._record(job, "incomplete", resume, "Naukri recruiter questions started")  # an attempt is under way
        for _ in range(MAX_CHAT_QUESTIONS):
            state = self.browser.chat_state()
            if state.kind == "applied":
                return self._confirmed(job, resume, state.reason or "success_message", "Naukri confirmed the application")
            if state.kind == "login":
                return self._result(job, "requires_login", AUTH_REQUIRED, "Naukri asked for login during questions",
                                    resume=resume)
            if state.kind == "gone":
                return self._confirm_after_chat(job, resume)
            if state.kind in ("blocked", "error"):
                stop = self._manual_question(job, resume, state, state.code, state.reason)
                if stop:
                    return stop
                continue
            question = state.question
            if question.kind == "file":
                stop = self._upload_in_chat(job, resume, state)
                if stop:
                    return stop
                continue
            decision = questions.decide_answer(question.text, self.profile, self.evidence, question.kind,
                                               question.options or None)
            logger.info(f"Question | db_id={job['id']} | category={decision.category.value} | "
                        f"source={decision.source or '-'} | action={'auto_answered' if decision.automatic else 'manual'}")
            if not decision.automatic:
                stop = self._manual_question(job, resume, state, decision.code, decision.reason)
                if stop:
                    return stop
                continue
            try:
                self.browser.answer_chat(question, decision.value)
            except Exception as e:
                stop = self._manual_question(job, resume, state, QUESTION_FORM_CHANGED, f"could not enter the answer: {e}")
                if stop:
                    return stop
                continue
            after = self.browser.wait_for_chat_change(state.signature)
            if after.signature == state.signature:
                stop = self._manual_question(job, resume, state, QUESTION_FORM_CHANGED,
                                             "Naukri did not move on after Save (answer not accepted?)")
                if stop:
                    return stop
                continue
            self._set(job, "submitting", questions.QUESTION_AUTO_ANSWERED,
                      f"auto-answered a {decision.category.value} question")
        return self._result(job, "requires_manual_action", QUESTION_FORM_CHANGED,
                            f"more than {MAX_CHAT_QUESTIONS} questions; finish in the browser", resume=resume)

    def _upload_in_chat(self, job: dict, resume: ResumeInfo, state: ChatState) -> Optional[ApplicationResult]:
        """Naukri asked for a resume file: upload the validated job-specific file, or hand it over.
        Never the master resume, never markdown, never a file for another job."""
        if not resume.uploadable:
            return self._manual_question(job, resume, state, resume.upload_code or RESUME_PDF_NOT_FOUND,
                                         f"Naukri asks for a resume file but there is no validated job-specific "
                                         f"PDF ({resume.upload_reason})")
        try:
            self.browser.upload_resume(resume)
        except UploadError as e:
            return self._manual_question(job, resume, state, e.code, f"resume upload: {e}")
        logger.info(f"Resume uploaded | db_id={job['id']} | resume_id={resume.resume_id} | v{resume.version} | "
                    f"{resume.path.suffix[1:]}")
        try:
            self.browser.click_chat_save(state.question)
        except Exception as e:
            return self._manual_question(job, resume, state, QUESTION_FORM_CHANGED, f"could not save the upload: {e}")
        if self.browser.wait_for_chat_change(state.signature).signature == state.signature:
            return self._manual_question(job, resume, state, RESUME_UPLOAD_NOT_CONFIRMED,
                                         "the resume was attached but Naukri did not move on")
        self._set(job, "submitting", RESUME_UPLOADED, f"uploaded verified resume v{resume.version}")
        return None

    def _manual_question(self, job: dict, resume: ResumeInfo, state: ChatState, code: Optional[str],
                         reason: str) -> Optional[ApplicationResult]:
        """Hand one question to the human. None = it was resolved, continue; else the stop result."""
        self._set(job, "requires_manual_action", code, reason)
        line = "=" * 50
        self.out(f"\n{line}\nMANUAL APPLICATION QUESTION\n{line}\n\nQuestion:\n{state.text or '(not readable)'}\n\n"
                 f"This question cannot be answered automatically ({reason}).\n\n"
                 f"Please answer it in the open Naukri browser.\n\n"
                 f"Press ENTER here when you have completed the question.\n{line}")
        while True:
            if self._ask("Press ENTER when done (s = stop for now): ", "s") in ("s", "q"):
                self._record(job, "incomplete", resume, reason)
                return self._result(job, "requires_manual_action", code, reason, resume=resume)
            now = self.browser.chat_state()
            if now.kind != state.kind or now.signature != state.signature:
                self._set(job, "submitting", None, "continuing after a manual answer")
                return None
            self.out("The question still appears unanswered in the browser. Answer it there, then press ENTER.")

    def _confirm_after_chat(self, job: dict, resume: ResumeInfo) -> ApplicationResult:
        """The chat panel closed: wait, re-open the job and trust only Naukri's own confirmation."""
        return self._unconfirmed(job, resume, "the question panel closed without a confirmation")

    def _manual_handoff(self, job: dict, resume: ResumeInfo, code: str, reason: str) -> ApplicationResult:
        """Let the human finish in the open browser; record applied only if Naukri then confirms it."""
        self._set(job, "requires_manual_action", code, reason)
        self.out(f"Manual action needed: {reason}\nFinish (or cancel) the application yourself in the browser window.")
        self._ask("Press Enter when you are done (or s to leave it for later): ", "s")  # either way: re-check only
        confirmation = detect_application_confirmation(self.browser.open_job(job["url"]))
        if confirmation.confirmed:
            return self._confirmed(job, resume, confirmation.signal, "completed manually; Naukri confirms the application")
        self._record(job, "incomplete", resume, reason)
        return self._result(job, "requires_manual_action", code, reason, resume=resume)

    # -- recovery ---------------------------------------------------------------------------

    def recover(self, job: dict) -> ApplicationResult:
        """Human-driven recovery of an interrupted/unconfirmed application. Read-only on Naukri:
        never clicks Apply, never answers or submits anything.

        Naukri shows Applied -> already_applied (history upgraded). Naukri shows its Apply button
        -> after the user's explicit 'y', back to 'ready' (a new application still needs a fresh 'y').
        Anything else (login, CAPTCHA, unknown page) -> stays recovery_required.
        """
        self._begin()
        self._event("RECOVERY_START", job)
        self.idempotency(job)  # closes attempts a crashed run left open (recorded, not retried)
        self._attempt_id = start_attempt(job, "recovery")
        resume, _, _ = resolve_resume(job["url"], self.agent_output_dir)
        try:
            assessment = self.verify(job)
        except ApplicationStop as stop:
            self._result(job, "recovery_required", stop.code, f"recovery stopped: {stop}")
            raise
        except Exception as e:
            return self._result(job, "recovery_required", NETWORK_TIMEOUT,
                                f"recovery could not load the job page ({type(e).__name__}); try again later")
        confirmation = detect_application_confirmation(self.browser.snapshot())
        self._event("RECOVERY_RESULT", job, assessment.state, signal=confirmation.signal)
        if assessment.state == "already_applied" or confirmation.confirmed:
            record = get_application_record(job)
            if record and record.get("status") != "applied":
                record_application_attempt(job, "applied", record.get("resume_path"), job["url"],
                                           "confirmed on Naukri during recovery")
            return self._result(job, "already_applied", CONFIRMED_DURING_RECOVERY,
                                "recovery: Naukri shows this job as applied", resume=resume,
                                confirmation_signal=confirmation.signal or "applied_state")
        if assessment.state == "requires_login":
            self._result(job, "recovery_required", AUTH_REQUIRED, f"recovery needs a Naukri login: {assessment.reason}",
                         resume=resume)
            raise ApplicationStop(AUTH_REQUIRED, "Naukri login required. Run `python main.py --action naukri-login`, "
                                  "log in by hand, then run recover-application again.")
        if assessment.state in ("job_unavailable", "external_application"):
            return self._result(job, assessment.state, assessment.code, f"recovery: {assessment.reason}",
                                external_url=assessment.external_url, resume=resume)
        if assessment.state != "ready":  # login, CAPTCHA, unknown page: ambiguous -> stay in recovery
            return self._result(job, "recovery_required", assessment.code or UNKNOWN_UI_STATE,
                                f"recovery could not establish the outcome: {assessment.reason}", resume=resume)
        self.out("Naukri shows this job as NOT applied: its Apply button is available and there is no "
                 "Applied state or confirmation.")
        if self._ask("Return this job to the application queue? A new application will still need your "
                     "explicit 'y' at the review. [y/n]: ", "yn") != "y":
            return self._result(job, "recovery_required", job.get("application_code") or "RECOVERY_REQUIRED",
                                "left in recovery by the user", resume=resume)
        resolve_unconfirmed_record(job, "recovery: Naukri showed its Apply button (not applied)")
        return self._result(job, "ready", RECOVERED_NOT_APPLIED,
                            "recovery: Naukri shows the job as not applied; requeued by the user", resume=resume)


__all__ = [
    "ApplicationRunner",
    "ApplicationStop",
    "Assessment",
    "NaukriApplicationBrowser",
    "ResumeInfo",
    "assess_job_page",
    "classify_auth",
    "choose_apply_control",
    "classify_question",
    "find_tailored_resume",
    "format_review",
    "plan_answers",
    "validate_resume_file",
]
