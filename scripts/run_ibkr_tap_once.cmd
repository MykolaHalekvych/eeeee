@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%\..") do set "REPO=%%~fI"
pushd "%REPO%" >nul

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%PS%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SCRIPT_DIR%ibkr_tap_once.ps1"

set "RC=%ERRORLEVEL%"
popd >nul
exit /b %RC%
