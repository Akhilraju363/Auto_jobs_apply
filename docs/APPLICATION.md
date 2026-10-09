# Naukri application preparation (Phase 3)

Phase 3 turns jobs that the Job Application AI Agent scored as `recommended` into prepared
Naukri applications. **A human approves every single submission.** Nothing is ever applied
in the background.

```text
recommended job (jobs.db)
  -> tailored resume check (AI Agent output, read-only)
  -> open the job in the headed browser with your saved Naukri session
  -> verify: logged in? CAPTCHA? job still open? already applied? Naukri Apply or company site?
  -> review screen (score, matched/missing skills, resume, URL)
  -> you type y            <- the only way anything is submitted
  -> click Naukri's own Apply button
  -> confirmation / questions / manual hand-off
  -> result recorded in jobs.db (+ applied_jobs for real submission attempts)
```

## Authentication

**Naukri credentials are never stored by this application. Authentication uses the locally
persisted browser session** in `chrome_user_data/` (git-ignored, never reset or cleared by
this tool).

First time (and whenever Naukri logs you out):

```bash
python main.py --action naukri-login
```

A browser window opens on Naukri's login page. Log in yourself, including any OTP or CAPTCHA.
The command only watches for the logged-in page header and then closes the window. It never
types, reads or saves your password. Waiting time: `NAUKRI_LOGIN_WAIT_MINUTES` (default 10).

A job counts as "logged in" only when Naukri's Login/Register links are absent **and** the
logged-in header is present. Anything uncertain is treated as `requires_login`, and nothing is
submitted.

## Commands

```bash
python main.py --action application-candidates            # recommended jobs ready for preparation
python main.py --action naukri-login                      # one-time manual login (saved profile)
python main.py --action verify-job --job-id 2             # check one job; never clicks Apply
python main.py --action apply --job-id 2 --dry-run        # full preparation + review, nothing clicked/recorded
python main.py --action apply --job-id 2                  # one job, asks for approval before Apply
python main.py --action apply                             # up to APPLICATION_MAX_JOBS jobs, one approval each
```

`--include-review` adds `review` (6-7/10) jobs; by default only `recommended` (8+/10, the AI
Agent's cutoff) jobs are candidates. `--job-id` is the `jobs.db` row id shown by
`application-candidates`.

Candidates: `status = scored`, `match_status = recommended`, not flagged applied by Naukri,
no successful `applied_jobs` record, and not in a final state (`applied`, `already_applied`,
`external_application`, `job_unavailable`). Retryable states (`requires_login`,
`requires_manual_action`, `failed`, `ready`) stay candidates.

## Approval

The review screen ends with `Approve this application? [y] Yes [n] No [s] Skip [q] Quit`.

- Only an exact `y` / `yes` approves. Enter, `n`, `s`, anything else, or a closed input
  (EOF/Ctrl+C) never approves.
- Approval is asked **immediately before clicking Naukri's Apply**, because Naukri's own Apply
  can submit on the first click using the resume on your Naukri profile.
- Each job is approved separately. There is no approve-all mode.
- If a form appears and every question can be answered from your configured facts, the filled
  answers are shown and a **second** approval is required before the form is submitted.

`--dry-run` opens and checks the job, finds the resume and shows the review. It never clicks
Apply and never writes to the database.

## Resume (Phase 3.2: verified PDF handoff)

**Only verified job-specific resume artifacts may be uploaded to Naukri. The master resume is
never automatically substituted.**

The AI Agent owns resume content, tailoring, fact checking and versioning. It publishes each
verified tailored resume as a local file next to its markdown:

```text
<AI Agent>/output/generated_resumes/<resume id>/
    meta.json   job.job_key (canonical job link), versions[n].validation.ok,
                versions[n].exports.pdf, versions[n].artifacts.pdf {version, verified, md_sha1, job_key}
    v1.md       the verified tailored resume
    v1.pdf      rendered from exactly v1.md (the Agent's Google Docs export), published only if v1 passed verification
```

The Agent's scheduled pipeline publishes the PDF for every newly tailored job, and the Drive copy is
that same file. For a job tailored before this existed, publish it once from the AI Agent folder:

```bash
python scripts/resume_artifacts.py --resume-id 810b8ec2012d   # or --link <job url>
```

This project only reads that folder (`AI_AGENT_OUTPUT_DIR`): nothing is generated, converted or
downloaded from Drive here. `resume_artifact.resolve_verified_resume()` follows the job's
canonical link to the Agent's saved record (`tailored_jobs.json` -> `resume_id` + version), then:

| Check | Fails with |
|---|---|
| a saved/verified resume exists for this job | `RESUME_NOT_FOUND` (Drive link shown when only Drive has it) |
| the version passed the Agent's verification | `RESUME_NOT_VERIFIED` |
| the record and the PDF were made for this job (canonical link) | `RESUME_ARTIFACT_MISMATCH` (the job stops) |
| `v<n>.pdf` (or `.docx`) exists for that version | `RESUME_PDF_NOT_FOUND` |
| it is a real PDF/DOCX that parses | `RESUME_PDF_INVALID` |
| it has meaningful text, every section heading of `v<n>.md` and >=90% of its wording, and was rendered from the current `v<n>.md` (`md_sha1`) | `RESUME_PDF_VALIDATION_FAILED` |

Priority: validated PDF, then validated DOCX, then the markdown (review only, never uploaded).
A Drive link is shown for information only. Check any job without opening a browser:

```bash
python main.py --action verify-resume --job-id 2
```

Naukri's in-platform Apply sends the resume on your **Naukri profile**. The tailored file is
uploaded only after your approval, and only when Naukri's application panel (chat or form)
asks for a file:
- The upload control is resolved like the Apply button: one application panel; the file input
  enabled, in a visible part of the panel, and accepting the file type. With several, only the
  single one labelled resume/CV is used. Otherwise `RESUME_UPLOAD_FORM_CHANGED` and nothing is
  uploaded.
- The file is uploaded as `<Name>_Resume.pdf` (never `v1.pdf`). The upload counts only when Naukri
  shows the file name or an upload confirmation, otherwise `RESUME_UPLOAD_NOT_CONFIRMED`. The
  chat continues only when Naukri moves on.
- No validated file, or any upload problem: the question is handed to you (same
  MANUAL APPLICATION QUESTION flow as other questions).
- `--dry-run` reports `Verified resume`, `PDF available` and `Upload candidate`, but never
  uploads.

## Questions (Phase 3.1)

**Only explicitly configured applicant facts may be automatically submitted as answers.
Unknown, unsupported, legal, sensitive, and free-text questions require human intervention.**

After you approve and Apply is clicked, Naukri may ask recruiter questions, one at a time in a
chat panel (question, answer box or choices, Save), or as a form. Each question is classified
deterministically (`application_questions.py`, no LLM) and then:

| Category | Answered from | Example wording recognised |
|---|---|---|
| `expected_ctc` | `APPLICANT_EXPECTED_CTC` | expected CTC/salary/compensation/package, "salary expectations", "what salary are you expecting" |
| `current_ctc` | `APPLICANT_CURRENT_CTC` | current CTC/salary/compensation, "currently earning" |
| `total_experience` | `APPLICANT_TOTAL_EXPERIENCE` | total/overall experience, "how many years of experience do you have" (no skill named) |
| `notice_period` | `APPLICANT_NOTICE_PERIOD` | notice period, "how soon/when can you join" |
| `current_location` | `APPLICANT_CURRENT_LOCATION` | current location/city, "where are you located" |
| `preferred_location` | `APPLICANT_PREFERRED_LOCATION` | preferred location |
| `phone` / `email` | `APPLICANT_PHONE` / `APPLICANT_EMAIL` | mobile/contact number, email |
| `willing_to_relocate` | `APPLICANT_WILLING_TO_RELOCATE` (true/false) | relocate |
| `work_from_office` | `APPLICANT_WORK_FROM_OFFICE` (true/false) | work from office, WFO, onsite, hybrid |
| `work_night_shift` | `APPLICANT_WORK_NIGHT_SHIFT` (true/false) | night/rotational shifts |
| `work_weekends` | `APPLICANT_WORK_WEEKENDS` (true/false) | weekends, Saturdays/Sundays |
| `technical_skill` | master resume | "Do you have experience with Spring Boot?" -> **Yes** only if the master resume names it |

Configure in `.env` (never in `.env.example` or code):

```env
APPLICANT_EXPECTED_CTC=12          # as the field expects it, e.g. 12 or 12 LPA (lakhs per annum)
APPLICANT_NOTICE_PERIOD=30 days    # or 30, Immediate, 2 months
APPLICANT_CURRENT_LOCATION=Hyderabad
APPLICANT_WILLING_TO_RELOCATE=true # true/false; empty = you decide each time
```

Values are entered exactly as configured. They are checked (a CTC must be a number, optionally
followed by LPA/lakhs; notice must be days/weeks/months or Immediate; ...) but never converted.
true/false become Yes/No. For choices, the answer must equal one of Naukri's options, otherwise
you answer.

How guessing is avoided:
- An empty setting is never filled from anything else. No expected CTC from the current CTC,
  no job salary range, no defaults, no AI, and no resume. The code is `UNCONFIGURED_ANSWER`, and
  you answer.
- Skill questions are answered **Yes** only when every named skill appears in the master resume
  (the AI Agent's `resume/base_resume.md`, override with `MASTER_RESUME_PATH`). They are never
  answered **No**, never with a number of years or a rating, and never from the job
  description or the tailored resume.
- Always yours: free text (why/describe/tell us/career goals/...), work authorization/visa/
  citizenship, demographic (gender, age, disability, veteran, ...), identity documents, legal
  declarations/consent/background checks/conflicts of interest, education, employment
  history, checkboxes, text areas, file uploads, and anything unrecognised.
- "Skip this question" is never clicked automatically.

Manual intervention shows:

```text
==================================================
MANUAL APPLICATION QUESTION
==================================================
Question:
Why are you interested in this role?
This question cannot be answered automatically (...).
Please answer it in the open Naukri browser.
Press ENTER here when you have completed the question.
==================================================
```

After Enter, the chat panel is checked again and the flow continues only once that question has
really been replaced (next question, confirmation, or the panel closing). Type `s` to stop: the
job stays `requires_manual_action` with an `incomplete` attempt recorded.

The initial approval still comes first. Answering configured questions after Apply needs no
further approval. A classic form with a final Submit still asks `Submit these answers?` first.
`Save` only means that one question was answered. The job becomes `applied` only when Naukri
shows a confirmation or the job page shows "Applied". If the chat closes without that, the
attempt is `unconfirmed` and the state is `UNKNOWN_UI_STATE`.

Question codes: `QUESTION_AUTO_ANSWERED`, `QUESTION_MANUAL_REQUIRED`, `UNCONFIGURED_ANSWER`,
`UNSUPPORTED_QUESTION`, `QUESTION_FORM_CHANGED` (duplicate panels/inputs/Save buttons, answer not
accepted), `QUESTION_INPUT_NOT_FOUND`, `QUESTION_SAVE_NOT_FOUND`. Logs show the question category,
the answer source and the action, never the answer value, phone, email, salary or location.

## External applications

Jobs with "Apply on company site" (Workday, Greenhouse, Lever, career portals, ...) are marked
`external_application` with the URL when Naukri exposes it (otherwise the Naukri job URL). They
are never clicked or submitted. Apply there yourself.

## CAPTCHA and blocks

If Naukri shows a CAPTCHA/verification/"Access Denied" page, the job becomes
`requires_manual_action` (`CAPTCHA_REQUIRED`):

```text
CAPTCHA/manual verification required.
Complete the verification manually in the browser.
Then resume the application workflow.
```

Press Enter after solving it in the browser to re-check, or `q` to stop. Nothing is solved or
bypassed automatically.

## States and codes

`jobs.application_state`: `not_ready`, `ready`, `preparing`, `review_required`, `approved`,
`submitting`, `applied`, `failed`, `requires_login`, `requires_manual_action`, `already_applied`,
`external_application`, `job_unavailable`. `jobs.application_code` / `application_reason`
explain the last outcome: `AUTH_REQUIRED`, `CAPTCHA_REQUIRED`, `JOB_NOT_FOUND`, `JOB_CLOSED`,
`ALREADY_APPLIED`, `EXTERNAL_APPLICATION`, `RESUME_NOT_FOUND`, `UNSUPPORTED_QUESTION`,
`APPLICATION_FORM_CHANGED`, `SUBMISSION_FAILED`, `NETWORK_TIMEOUT`, `UNKNOWN_UI_STATE`,
`BROWSER_PROFILE_LOCKED`, `NOT_APPROVED`, `APPLIED`, and from Phase 3.3 `recovery_required` with
`SUBMISSION_UNCONFIRMED`, `INTERRUPTED_AFTER_APPLY`, `INTERRUPTED_BEFORE_APPLY`, `RECOVERED_NOT_APPLIED`,
`CONFIRMED_DURING_RECOVERY`.

`applied_jobs` keeps one row per job for genuine submission attempts (after Apply was clicked):
`applied`, `unconfirmed`, `incomplete`, `submission_failed`, `external_application` or
`not_applied` (recovery saw Naukri's Apply button), plus `resume_path`, `application_url`,
`failure_reason`, `external_url` and `job_row_id`. A job already recorded as `applied` is never
written again. Opening, verifying or declining a job never creates a row.

## Outcome tracking, idempotency and recovery (Phase 3.3)

**APPLIED means verified application confirmation, not merely a click.**
**Ambiguous submission is never automatically retried.**

```text
discovered -> scored (recommended) -> ready -> review_required -> approved -> submitting
           -> applied                (Naukri confirmed: success message, or Applied in the job header)
           -> already_applied        (Naukri already shows Applied / local history says applied)
           -> external_application   (company site; never clicked)
           -> recovery_required      (Apply may have been clicked, no confirmation: needs recover-application)
before Apply: requires_login, requires_manual_action (CAPTCHA, resume, unknown page), failed (Apply
provably not clicked), job_unavailable, ready (not approved)
```

**Confirmation** (`application_outcome.detect_application_confirmation`) accepts only:
- an explicit success message ("successfully applied", "applied successfully", "your application
  has been submitted", ...), or
- Naukri's own Applied control in the job header. Naukri renders it as
  `<span id="already-applied">Applied</span>`, so it is read by id, not just from buttons.

It never accepts a URL change, a disappeared Submit button, a finished request, a "Save" in the
question chat, or an "Applied" link outside the job header (navigation). Before giving up it waits
`CONFIRMATION_WAIT_SECONDS` (default 20) and re-opens the job page (read-only) once.

**Attempt history** (`application_attempts`, one row per attempt, written ahead):
- A row starts when you approve. `apply_clicked` is written **before** Naukri's Apply is clicked, so
  a crash can never hide a click.
- On completion the row gets its state, code, reason, resume path/version, URL, external URL,
  confirmation signal, success flag and duration. A completed row is never changed again.
  Recovery inspections are rows of kind `recovery`.

**Idempotency** (checked before anything opens):

| Situation | Result |
|---|---|
| `applied_jobs` says applied | `already_applied`, nothing opened |
| an open attempt with `apply_clicked` (the run died after the click) | `recovery_required` (`INTERRUPTED_AFTER_APPLY`) |
| an open attempt without a click (died before Apply) | closed as `INTERRUPTED_BEFORE_APPLY`; the job continues (approval still needed) |
| history `unconfirmed` / `incomplete` / `submission_failed` | `recovery_required` (`SUBMISSION_UNCONFIRMED`) |
| state `recovery_required`, or an older run's post-Apply code (questions, upload, `submitting`) | `recovery_required` |
| Naukri shows Applied when the job is opened | `already_applied`, nothing clicked |

Jobs in `recovery_required` are not application candidates. `apply` refuses them (also in
`--dry-run`) and points to recovery.

**Retry rules.** Only page loads *before* Apply are retried (`NAUKRI_NAVIGATION_RETRIES`, default
2). Nothing after Apply is ever retried automatically: submission timeouts, browser crashes,
unknown UI, changed forms, CAPTCHA, login, and ambiguous or failed confirmations all stop with
`recovery_required`. If Apply is provably not clicked (the resolver refused), the result is `failed`
(`APPLICATION_FORM_CHANGED`) and the job may be tried again later.

**Commands**

```bash
python main.py --action application-status                 # jobs needing attention, grouped by state (--max-jobs N)
python main.py --action application-status --job-id 2      # state, code, last attempt, history, resume, score, idempotency verdict
python main.py --action recover-application --job-id 2     # read-only check on Naukri, then you decide
```

`recover-application` opens the job with your saved session and never clicks Apply, answers or
submits anything:
- Naukri shows **Applied**: `already_applied` (`CONFIRMED_DURING_RECOVERY`); unconfirmed history
  is upgraded to `applied`.
- Naukri shows its **Apply** button and no Applied/confirmation: you are asked "Return this job to
  the application queue?". Only `y` requeues it (`ready`, `RECOVERED_NOT_APPLIED`, history becomes
  `not_applied`). The next `apply` shows the review again and needs a fresh `y`.
- Login, CAPTCHA, closed/unknown page: the job stays `recovery_required`. Nothing is guessed.

**Diagnostics.** On a failure, a sanitized `state.json` is written to
`APPLICATION_DEBUG_DIR/<job id>/<timestamp>/` (default `output/application_debug/`, git-ignored,
`APPLICATION_DEBUG=false` to disable). It contains the URL without query, page title, login/header
flags, detected apply/applied controls, the job header's control labels, structure counts
(chat/form/inputs/Save/file inputs), state, code, reason and resume metadata. It never contains
page body text, answers, cookies, tokens or screenshots. Configured `APPLICANT_*` values, emails,
phone numbers and salary amounts are masked.

**Events.** One log line per transition from the `application_events` logger, with job id, Naukri
id, canonical URL, event, state and timestamp: `APPLICATION_START`, `JOB_OPENED`,
`JOB_STATE_CHECKED`, `REVIEW_SHOWN`, `APPROVAL_RECEIVED`, `APPLY_CLICKED`, `SUBMISSION_PENDING`,
`CONFIRMATION_DETECTED`, `APPLICATION_CONFIRMED`, `APPLICATION_FAILED`, `RECOVERY_REQUIRED`,
`RECOVERY_START`, `RECOVERY_RESULT`, `IDEMPOTENCY_BLOCKED`. No answers or personal values.

**Dry run** additionally reports the current state, the idempotency verdict, the recovery status,
the resume artifact, which facts would be auto-answered and that approval is required. It never
clicks, uploads, answers, submits, records or writes diagnostics.

**Live validation procedure** (one job only):
1. `python -m pytest tests -q`
2. `python main.py --action application-status --job-id <id>`. It must say the idempotency check
   allows a new application.
3. `python main.py --action verify-resume --job-id <id>`
4. `python main.py --action apply --job-id <id> --dry-run`
5. `python main.py --action apply --job-id <id>`, then type `y` only if the review is right.
6. If the result is `recovery_required`: stop. Do not retry. Run `application-status`, then
   `recover-application`.

Limitations: confirmation text and controls are Naukri's current UI. If Naukri changes them,
results become `recovery_required` (never a false `applied`). A job applied outside this tool shows
up as `already_applied` without an `applied_jobs` row.

## Google Sheet

Application state is authoritative in SQLite (`data/naukri_auto_apply.db` since Phase 4). Since
Phase 4, `apply` and `recover-application` queue each outcome in the `sheet_sync` table and the
pipeline writes it to the AI Agent's tracker sheet (updating the Agent's own row for the job, Status
`Applied` once confirmed). Sync now with `python application_pipeline.py --sync-only`. A Sheets
failure only leaves the row pending. Details: [PIPELINE.md](PIPELINE.md#google-sheets).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `AUTH_REQUIRED` | `python main.py --action naukri-login`, log in by hand, rerun |
| `BROWSER_PROFILE_LOCKED` | close other windows/processes using `chrome_user_data/` (a running scan) |
| `RESUME_NOT_FOUND` | run the AI Agent for the job, or export the PDF from its dashboard; check `AI_AGENT_OUTPUT_DIR` |
| `UNKNOWN_UI_STATE` / `APPLICATION_FORM_CHANGED` | Naukri's page changed or was ambiguous; apply by hand, then rerun to record it |
| `CAPTCHA_REQUIRED` | solve it in the browser, press Enter; or wait and retry later |

## Configuration (.env)

```env
NAUKRI_APPLICATION_TIMEOUT=30    # seconds per page wait
APPLICATION_MAX_JOBS=1           # jobs per `--action apply` run
NAUKRI_LOGIN_WAIT_MINUTES=10
APPLICANT_TOTAL_EXPERIENCE=      # optional factual answers; leave empty to always answer yourself
APPLICANT_NOTICE_PERIOD=
...
```

There is no username/password setting, and none will be added.

## Not part of Phase 3

- No automatic login, OTP or CAPTCHA handling. No proxies, fingerprinting or other evasion.
- No submissions to external ATS sites.
- No generated free-text answers.
- No batch approval.
- The legacy `python main.py --mode cli` auto-apply loop (`NaukriDriver.search_and_apply_jobs`)
  predates Phase 3 and does **not** have the approval gate; use `--action apply` instead.
## Eligibility and freshness

The application layer considers only jobs with `freshness_status=FRESH` inside
the configured 24-hour window. Missing or ambiguous freshness is never treated
as fresh. Location and experience are matched only from explicit configured
preferences and available job/profile data; relocation is not inferred.

Scores of 8--10 are application candidates, 6--7 are review-only, and 5 or
below are skipped. A verified job-specific PDF/DOCX and clean application
history are required. Recovery-required, already-applied, successful, duplicate,
or in-progress jobs remain blocked. Candidate priority is informational only.
Previous applications may provide reference metadata, but their free-text,
demographic, legal, sensitive, and unsupported technical answers are never
copied automatically. The master resume is the factual source of truth.

Use `python main.py --action fresh-jobs --max-jobs 10` to list fresh stored
jobs without opening an application form, and
`python main.py --action application-candidates` to list human-review
candidates. Dry-run remains non-submitting. Exact `y`/`yes` is still required
before final submission.
