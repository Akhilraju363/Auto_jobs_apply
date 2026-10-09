@echo off
rem Naukri Auto Job Apply - one local run of the full workflow (application_pipeline.py).
rem Double-click it, or run it from any folder. Extra arguments are passed to the pipeline, e.g.
rem   start_local.bat --dry-run        open and check jobs, never click Apply
rem   start_local.bat --no-apply       scan + score + eligibility only
rem   start_local.bat --sync-only      only retry pending Google Sheets rows
rem Never installs packages, never touches chrome_user_data\ and never resets the Naukri login.
rem Exit codes: 0 run complete, 1 setup/configuration problem, 2 finished but needs your attention.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo [FAIL] .venv not found in %CD%
    echo        Create it once:  python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    set "EXITCODE=1"
    goto :done
)
call ".venv\Scripts\activate.bat"

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Python 3.11 or newer is required in .venv
    set "EXITCODE=1"
    goto :done
)
for /f "delims=" %%v in ('python --version 2^>^&1') do echo [OK]   %%v

python -c "import playwright.sync_api, dotenv, pypdf" >nul 2>&1
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

python application_pipeline.py --check-config
if errorlevel 1 (
    echo [FAIL] Configuration is not valid; nothing was run.
    set "EXITCODE=1"
    goto :done
)

echo.
python application_pipeline.py %*
set "EXITCODE=%ERRORLEVEL%"

:done
rem Opened by double-click: keep the window so the summary stays readable.
echo %cmdcmdline% | "%SystemRoot%\System32\find.exe" /i "%~nx0" >nul && (
    echo.
    echo start_local.bat finished with exit code %EXITCODE%.
    pause
)
exit /b %EXITCODE%
