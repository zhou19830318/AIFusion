@echo off
rem ---------------------------------------------------------
rem  Fusion360-Add-Ins (AI Fusion) - One-click Installer
rem ---------------------------------------------------------
title Fusion360-Add-Ins (AI Fusion) - One-click Installer

setlocal enabledelayedexpansion

rem ---- Determine APPDATA / Roaming path ----
if defined APPDATA (
  set "ADDINS=%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns"
) else (
  set "ADDINS=%USERPROFILE%\AppData\Roaming\Autodesk\Autodesk Fusion 360\API\AddIns"
)
if not exist "%ADDINS%" mkdir "%ADDINS%"
set "TARGET=%ADDINS%\Fusion360-Add-Ins"

rem Current directory (script location)
set "CURRENT=%~dp0"
if "%CURRENT:~-1%"=="\" set "CURRENT=%CURRENT:~0,-1%"

echo [1/4] AddIns folder: %ADDINS%
echo.

rem ---- Check existing installation ----
set "FOUND="
if exist "%TARGET%\AIFusion.manifest" set "FOUND=1"
if not defined FOUND if exist "%CURRENT%\AIFusion.manifest" set "FOUND=1"
if not defined FOUND (
  for /d %%D in ("%ADDINS%\*") do (
    if not defined FOUND if exist "%%~D\AIFusion.manifest" set "FOUND=1"
  )
)

if defined FOUND (
  echo [2/4] Existing installation found: %TARGET%
  set "DEPLOY_DIR=%TARGET%"
) else (
  echo [2/4] No existing installation, copying files to: %TARGET%
  robocopy "%CURRENT%" "%TARGET%" /E /XD .git __pycache__ .inscode /XF aifusion_debug.log aifusion_log.txt log_tail.txt config.json *.tmp >nul
  if %errorlevel% GEQ 8 (
    echo [ERROR] Copy failed. Abort.
    pause
    exit /b 1
  )
  set "DEPLOY_DIR=%TARGET%"
)
echo.

rem ---- Ensure Python dependencies ----
echo [3/4] Checking Python & dependencies...
where python >nul 2>nul
if %errorlevel%==0 (
  python -m pip install --quiet flask requests >nul 2>nul
  if %errorlevel%==0 (
    echo OK: flask + requests installed
  ) else (
    echo [WARNING] pip install failed, continuing anyway
  )
) else (
  echo [WARNING] Python not found, skipping dependency install
)
echo.

rem ---- API Key setup ----
echo [4/4] Setting up API Key...
powershell -NoProfile -ExecutionPolicy Bypass -File "%CURRENT%\tools\setup_config.ps1" -DeployDir "%DEPLOY_DIR%"
echo.

rem ---- Finish ----
echo ============================================================
echo   Install complete!
echo ============================================================
echo.
echo   Steps to use:
echo   1. Open Fusion 360.
echo   2. Press Shift+S, go to Add-Ins, enable "Fusion360-Add-Ins".
echo        Set "Run on Startup" if desired.
echo   3. Enter your API key (if prompted).
echo.
echo   Deploy directory: %DEPLOY_DIR%
echo.
pause
endlocal