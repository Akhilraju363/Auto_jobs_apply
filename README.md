# Auto Job Apply

Auto Job Apply is a Python-based automation project for applying to jobs on LinkedIn and Naukri using a browser-driven workflow with a lightweight web dashboard, resume matching, and optional AI-powered screening responses.

It is designed to help you:

- search and apply to relevant jobs faster
- track applications locally in a SQLite database
- monitor activity through a Flask dashboard
- pause and resume automation when the browser needs human input
- optionally use a Chrome extension for resume autofill and form assistance

## Why this project exists

The goal is to reduce repetitive job-application work while keeping a human-in-the-loop checkpoint for scenarios such as CAPTCHA, OTP verification, or other manual browser prompts.

## Key features

- LinkedIn automation
- Naukri automation
- Resume-to-job matching using TF-IDF similarity
- Flask dashboard for live status monitoring
- SQLite application tracking
- Optional Gemini integration for screening answers
- Chrome extension support for quicker form filling
- Daily application limits and configurable job search settings

## Tech stack

- Python 3.12+
- Flask
- Playwright
- SQLite
- Google Gemini API
- Chrome/Chromium browser automation

## Repository structure

```text
.
├── app.py                # Flask web dashboard entry point
├── main.py               # CLI automation and diagnostics entry point
├── config.py             # Project configuration and environment settings
├── db.py                 # Database helpers
├── gemini_engine.py      # Gemini API integration
├── hitl_engine.py        # Human-in-the-loop pause/resume logic
├── linkedin_driver.py    # LinkedIn automation driver
├── naukri_driver.py      # Naukri automation driver
├── resume_matcher.py     # Resume/job similarity logic
├── resume.txt            # Your resume content
├── .env                  # Local secrets and API configuration
├── chrome_user_data/     # Chromium profile data
├── extension/            # Chrome extension source
└── templates/            # Dashboard HTML templates
```

## Prerequisites

Before running the project, make sure you have:

- Python 3.11 or newer (tested on Python 3.14)
- Google Chrome or Chromium installed
- Access to a terminal with pip available

## Installation

1. Clone the repository.
2. Create and activate a virtual environment if you want an isolated setup.
3. Install the dependencies:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Configuration

Copy `.env.example` to `.env` and set your values. `.env` and the browser profile
`chrome_user_data/` hold secrets/session cookies and are git-ignored; never commit them.

```env
GEMINI_API_KEY=your_api_key_here
RESUME_PATH=resume.txt
```

You should also update:

- the plain-text resume at `RESUME_PATH` (default `resume.txt` in the project folder; the
  committed file is only a sample). To keep your real resume out of git, point
  `RESUME_PATH` at a file outside the repository (absolute paths are allowed)
- `config.py` if you want to change the daily limit, matching threshold, or job search titles

## Running the project

### 1. Web dashboard

```bash
python main.py --mode web
```

Then open:

```text
http://localhost:5000
```

### 2. CLI automation on LinkedIn

```bash
python main.py --mode cli --platform linkedin --headless
```

### 3. CLI automation on Naukri

```bash
python main.py --mode cli --platform naukri --headless
```

### 4. Run both platforms

```bash
python main.py --mode cli --platform all --headless
```

### 5. Diagnostics check

```bash
python main.py --mode test
```

### 6. Naukri job discovery (scan only, never applies)

```bash
python main.py --platform naukri --action scan --max-pages 1 --max-jobs 5
```

Searches each keyword, follows pagination, opens every new job page, extracts the
full job description, skills and metadata, deduplicates (Naukri job ID → canonical
URL → title+company+location) and stores jobs in the `jobs` table of `jobs.db` with
`status = 'discovered'`. Settings come from `.env` (see `.env.example`):
`NAUKRI_ENABLED`, `NAUKRI_KEYWORDS` (comma-separated), `NAUKRI_LOCATION`,
`NAUKRI_EXPERIENCE`, `NAUKRI_MAX_PAGES`, `NAUKRI_MAX_JOBS`, `NAUKRI_DELAY_MIN`,
`NAUKRI_DELAY_MAX`. `--keywords "A,B"` overrides the keywords for one run.

Run it headed (no `--headless`): Naukri serves an "Access Denied" page to headless
Chromium, and the scanner stops instead of trying to get around it. If a login
page appears, log in manually in the opened window; the browser profile in
`chrome_user_data/` is reused next time.

Job lifecycle in the `jobs` table: `status` is `discovered` until scored, then `scored`
with `match_status` set to `recommended`, `review` or `skipped`. Scoring columns
(`match_score`, `match_status`, `match_reason`, `matching_skills`, `missing_skills`,
`experience_match`, `location_match`, `scored_at`) stay NULL until
`db.save_job_score()` writes them.

Stored jobs can be read for matching with `db.get_jobs(status="discovered")`, or
programmatically: `NaukriScanner(...).scan().jobs`.

Tests: `python -m pytest tests` (or `python -m unittest`).

### 7. AI scoring via the Job Application AI Agent (file handoff)

Scoring, resume tailoring, company research and the Google Sheet tracker belong to the separate
**Job Application AI Agent** repository. This project does not score jobs itself; it hands
discovered Naukri jobs to the Agent as a JSON file and reads the Agent's scores back. The two
repositories stay separate and share only two file paths.

```text
scan -> jobs.db (discovered) -> export-to-agent -> exports/naukri_jobs.json
     -> AI Agent: scrape_jobs (Naukri source) -> score_jobs -> tailor_job -> company_research -> write_sheet
     -> AI Agent output/scored_jobs.json -> import-scores -> jobs.db (scored)
```

Setup (`.env` in this project, see `.env.example`):

```env
AI_AGENT_OUTPUT_DIR=C:/path/to/job-application-ai-agent/output
NAUKRI_EXPORT_PATH=exports/naukri_jobs.json
AI_AGENT_EXPORT_LIMIT=2
```

In the AI Agent's `.env`, set `NAUKRI_JOBS_PATH` to this project's `exports/naukri_jobs.json`, and
`JOB_SOURCES=naukri` to skip its paid LinkedIn/Apify scrape (see the AI Agent's `docs/CONFIG.md`).

Run locally, in order:

```bash
# 1. this project
python main.py --platform naukri --action scan      # headed browser
python main.py --action export-to-agent             # at most AI_AGENT_EXPORT_LIMIT jobs (--max-jobs N overrides)

# 2. AI Agent (its own folder and venv; local mode uses Ollama: `ollama serve` must be running)
#    PowerShell: $env:LOCAL_MODE="true"; $env:JOB_SOURCES="naukri"; python scripts/run_pipeline.py
LOCAL_MODE=true JOB_SOURCES=naukri python scripts/run_pipeline.py

# 3. this project
python main.py --action import-scores
```

- **Export** selects only `status = 'discovered'` jobs with a title, company, URL and a description
  of at least 80 characters (the AI Agent's minimum). The `link` is the canonical Naukri URL
  (the AI Agent's `canonical_link` rule). A shorter description gets Naukri's own key skills
  appended as `Key skills: ...` in the exported file only; jobs still too short are skipped and
  listed. `jobs.db` is never changed by an export.
- **Import** matches results by canonical URL, falling back to `dedup_key`, and stores them
  with `db.save_job_score()`: `match_score = score x 10`; `match_status` is `recommended` for
  8-10 (the AI Agent's own qualification cutoff), `review` for 6-7, `skipped` for 1-5 (labels in
  this project only); `match_reason`, `matching_skills`, `missing_skills` come from the Agent;
  `experience_match` / `location_match` stay empty (the Agent does not produce them). Already
  scored jobs are never overwritten, so importing twice changes nothing. Jobs the Agent did not
  score stay `discovered` and are exported again next time.
- First run: keep `AI_AGENT_EXPORT_LIMIT=2`.
- Local only for now: the Naukri scan needs a headed browser with your saved profile, so it
  cannot run on the AI Agent's Modal deployment. Nothing here applies to jobs; a human still
  applies.

## Chrome extension

A bundled Chrome extension is included in the `extension/` folder. You can load it manually in Chrome using `chrome://extensions/` with Developer mode enabled, or use the provided `LOAD_EXTENSION.bat` helper.

The extension is useful for:

- filling forms faster
- reusing your resume data
- integrating Gemini-assisted autofill behavior

## Environment and data files

- `.env` contains local secrets and API settings
- `resume.txt` contains the resume text used for matching and autofill
- `jobs.db` stores application tracking data
- `chrome_user_data/` keeps browser profile state for persistent Chromium sessions

## Notes on usage

- The first run may require manual sign-in to LinkedIn or Naukri.
- Some sites may require CAPTCHAs, OTP verification, or a human confirmation step.
- The system pauses for human-in-the-loop intervention when needed.
- Daily application volume is capped by the configured limit.

## Safety and compliance

Use this project responsibly and only on websites where you have permission to automate job applications. Respect each platform's terms of use, rate limits, and anti-bot policies.

## License

This project is provided under the repository license included in the project root.

## Contributing

Pull requests and improvements are welcome. If you plan to make changes, keep the project documentation up to date and verify the behavior with the provided diagnostic mode whenever possible.

### 8. Application preparation with human approval (Phase 3)

Prepares Naukri applications for AI-Agent-`recommended` jobs. **Every submission needs your
explicit `y` at a review screen, immediately before Naukri's Apply is clicked.** Full guide:
[docs/APPLICATION.md](docs/APPLICATION.md).

```bash
python main.py --action application-candidates
python main.py --action naukri-login                  # once: log in by hand in the opened browser
python main.py --action verify-job --job-id <id>
python main.py --action apply --job-id <id> --dry-run
python main.py --action apply --job-id <id>
```

Naukri credentials are never stored by this application. Authentication uses the locally
persisted browser session in `chrome_user_data/`. CAPTCHAs, unsupported questions and
company-site applications are handed to you; nothing is bypassed or auto-answered beyond the
optional facts in `.env` (`APPLICANT_*`). `jobs.db` is git-ignored because it holds your
application history.

Note: the legacy `python main.py --mode cli` apply loop predates this and has no approval gate.

#### Application questions (Phase 3.1)

Only explicitly configured applicant facts may be automatically submitted as answers.
Unknown, unsupported, legal, sensitive, and free-text questions require human intervention.
Set the facts you are happy to have entered automatically in `.env`, for example
`APPLICANT_EXPECTED_CTC=12`, `APPLICANT_NOTICE_PERIOD=30 days`,
`APPLICANT_CURRENT_LOCATION=Hyderabad`, `APPLICANT_WILLING_TO_RELOCATE=true` (full list in
`.env.example`). Leave a setting empty and that question is always handed to you. Details,
recognised wordings and the manual-intervention flow: [docs/APPLICATION.md](docs/APPLICATION.md#questions-phase-31).

#### Verified resume upload (Phase 3.2)

Only verified job-specific resume artifacts may be uploaded to Naukri. The master resume is never
automatically substituted. The AI Agent publishes `generated_resumes/<id>/v<n>.pdf` from the verified
tailored resume; this project validates it (job, version, verification, content) and uploads it only
when Naukri asks for a file after your approval. Check a job with
`python main.py --action verify-resume --job-id <id>`. Details: [docs/APPLICATION.md](docs/APPLICATION.md).

#### Outcome tracking and recovery (Phase 3.3)

APPLIED means verified application confirmation, not merely a click. Ambiguous submission is never
automatically retried: once Apply may have been clicked without a Naukri confirmation, the job becomes
`recovery_required`, and `apply` refuses it until you run
`python main.py --action recover-application --job-id <id>` (a read-only check on Naukri). Use
`python main.py --action application-status [--job-id <id>]` to see states, attempts and what needs
attention. Details: [docs/APPLICATION.md](docs/APPLICATION.md).
## Phase 3.4 eligibility

Only **FRESH** jobs are automatic application candidates. Freshness is evaluated
against an explicit timezone-aware reference and the default
`APPLICATION_JOB_WINDOW_HOURS=24`; ambiguous labels such as `1 day ago` are
`UNKNOWN`, never fresh. Configure normalized `PREFERRED_LOCATIONS` (including
`Bangalore`/`Bengaluru` and `Remote`/`WFH`) and the applicant experience
through the existing profile settings. The AI Agent score remains authoritative;
the default application threshold is `APPLICATION_SCORE_THRESHOLD=8` (on its
1--10 scale).

Candidate selection also blocks recovery-required, already-applied, successful,
duplicate, or in-progress application history and requires a verified resume
artifact. Candidate priority is display-only. Previous applications are
reference data, not a source of unsupported claims; the master resume remains
the factual source of truth. `--action fresh-jobs` is read-only, and
`--dry-run` never clicks Apply, uploads, answers questions, or submits.
