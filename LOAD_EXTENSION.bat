@echo off
REM Chrome Extension Loader Script
REM This script opens Chrome and guides you to load the extension

echo.
echo ================================================
echo CHROME EXTENSION LOADER
echo ================================================
echo.
echo Opening Chrome Extensions page...
echo.

REM Open Chrome extensions page
start chrome://extensions/

timeout /t 2

echo.
echo ================================================
echo INSTRUCTIONS:
echo ================================================
echo.
echo 1. Chrome should have opened to chrome://extensions/
echo.
echo 2. In the TOP RIGHT corner:
echo    - Toggle "Developer mode" to ON (turns blue)
echo.
echo 3. Click the "Load unpacked" button
echo.
echo 4. In the folder picker dialog:
echo    - Navigate to DESKTOP
echo    - Open folder: Auto_job_apply
echo    - You should see the "extension" folder inside
echo    - CLICK ON THE "extension" FOLDER (don't open it, just select it)
echo    - Click "Select Folder"
echo.
echo ================================================
echo EXTENSION PATH:
echo ================================================
echo.
echo C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\extension
echo.
echo If Chrome asks for a path, copy and paste the above.
echo.
echo ================================================
echo.
pause
