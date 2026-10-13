@echo off
rem Naukri Auto Job Apply - one-click automatic job scan (local_launcher.py). Double-click it, or run
rem it from any folder. It takes no arguments and shows no menu: after the pre-flight checks it runs
rem SCAN AND SCORE ONLY - validate configuration (read-only), scan Naukri in the visible browser with
rem the saved profile (log in by hand there if Naukri asks), keep fresh 4+ years-eligible jobs, export
rem at most 2 to the AI Agent, score them (scrape_jobs.py + score_jobs.py only), save results and logs.
rem By default it NEVER submits an application, tailors a resume, uploads to Google Drive or writes Sheets.
rem OPT-IN: only a launcher_workflow.json containing exactly {"workflow": "auto-apply"} (created by you)
rem switches it to automatic applications (AUTO mode, this run's jobs only, APPLICATION_MAX_JOBS from .env,
rem still refused until one SAFE-mode application is confirmed). See docs\PIPELINE.md "Auto-apply".
rem Runs are recorded in output\runs\<run id>\ (launcher.log, run.log, result.json). See docs\PIPELINE.md.
rem Developer menu (options 1-5):  .venv\Scripts\python.exe local_launcher.py --menu
rem Developer CLI without the launcher:  .venv\Scripts\python.exe application_pipeline.py --help
rem Never installs packages, never touches chrome_user_data\ and never edits .env.
rem Exit codes: 0 OK, 1 setup/configuration problem or failed run, 2 finished but needs your attention.
setlocal
cd /d "%~dp0"
set "APPLICATION_MODE=SAFE"
set "artifact_sync=0"
set "GOOGLE_SHEETS_ENABLED=false"
set "JOB_SOURCES=naukri"
set "AI_AGENT_EXPORT_LIMIT=2"
set "APPLICATION_JOB_WINDOW_HOURS=24"
set "AI_AGENT_STAGES=scrape_jobs.py,score_jobs.py"
set "PY=%~dp0.venv\Scripts\python.exe"

if not "%~1"=="" (
    echo [FAIL] start_local.bat takes no arguments; the workflow comes only from launcher_workflow.json.
    echo        To run the pipeline directly, see "Developer CLI" in docs\PIPELINE.md.
    set "EXITCODE=1"
    goto :done
)

if not exist "%PY%" (
    echo [FAIL] .venv not found in "%CD%"
    echo        Create it once:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    set "EXITCODE=1"
    goto :done
)

"%PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Python 3.11 or newer is required in .venv
    set "EXITCODE=1"
    goto :done
)
for /f "delims=" %%v in ('call "%PY%" --version 2^>^&1') do echo [OK]   %%v

"%PY%" -c "import playwright.sync_api, dotenv, pypdf" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Required packages are missing. Install them once:
    echo        .venv\Scripts\pip install -r requirements.txt
    echo        .venv\Scripts\python -m playwright install chromium
    set "EXITCODE=1"
    goto :done
)
echo [OK]   Dependencies: playwright, python-dotenv, pypdf

if not exist ".env" (
    echo [FAIL] .env not found. Copy .env.example to .env and fill it in.
    set "EXITCODE=1"
    goto :done
)

"%PY%" "%~dp0local_launcher.py"
set "EXITCODE=%ERRORLEVEL%"

:done
rem Opened by double-click: keep the window so the summary stays readable.
echo %cmdcmdline% | "%SystemRoot%\System32\find.exe" /i "%~nx0" >nul && (
    echo.
    echo start_local.bat finished with exit code %EXITCODE%.
    pause
)
exit /b %EXITCODE%
