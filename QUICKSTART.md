# Quick Start Guide

## System Status
✓ All dependencies installed
✓ Flask app ready
✓ Database initialized
✓ Resume loaded
✓ Chrome automation configured

## Setup Instructions

### 1. Configure Gemini API (Optional but Recommended)
The system works without Gemini, but for AI-powered screening answers:

```bash
# Edit .env file and replace:
GEMINI_API_KEY=your_actual_api_key_here
```

Get your API key from: https://ai.google.dev/

### 2. Add Your Resume
The system comes with a sample resume. Replace it with yours:
- Edit `resume.txt` with your actual resume content

### 3. Run the System

#### Option A: Web Dashboard (Recommended)
```bash
python main.py --mode web
```
Then open: http://localhost:5000

#### Option B: CLI Mode (LinkedIn)
```bash
python main.py --mode cli --platform linkedin --headless
```

#### Option C: CLI Mode (Naukri)
```bash
python main.py --mode cli --platform naukri --headless
```

#### Option D: Both Platforms
```bash
python main.py --mode cli --platform all --headless
```

#### Option E: Run Diagnostics
```bash
python main.py --mode test
```

## Command Options

```bash
--mode       : web (dashboard), cli (terminal), test (diagnostics)
--platform   : all (default), linkedin, naukri
--headless   : Run browser in headless mode
--limit      : Max applications per day (default: 25)
--log-level  : DEBUG, INFO (default), WARNING, ERROR
```

## Features

✓ **LinkedIn Integration**: Auto-apply to matching jobs
✓ **Naukri Integration**: Auto-apply to matching jobs
✓ **Resume Matching**: Scores jobs against your resume (10% default threshold)
✓ **AI Screening**: Gemini-powered answers to screening questions
✓ **HITL (Human-in-the-Loop)**: Pauses for CAPTCHA/OTP
✓ **Web Dashboard**: Real-time monitoring of applications
✓ **Database Tracking**: All applications logged and tracked

## Troubleshooting

### Issue: ModuleNotFoundError
```bash
pip install -r requirements.txt
```

### Issue: Database locked
Delete `jobs.db` and restart

### Issue: Browser won't open
- Try with `--headless` flag disabled for debugging
- Check Chrome installation

### Issue: GEMINI_API_KEY error
- Not required for basic functionality
- Screening questions will be skipped
- Applications will still work

## Daily Limits
- Default: 25 applications per day
- Configurable with `--limit` flag
- Resets at midnight (based on system time)

## Data
- Applications logged in `jobs.db`
- Check web dashboard for history
- Each job: title, company, match score, timestamp

## Notes
- First run requires manual LinkedIn login
- Chrome user data stored in `chrome_user_data/`
- Logs show detailed execution flow
- MATCH_THRESHOLD: 10% (configurable in config.py)
