@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%\..") do set "REPO=%%~fI"
pushd "%REPO%" >nul

set "LOGDIR=%REPO%\args\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

set "OUT=%LOGDIR%\soak_report_24h_stdout.log"
set "ERR=%LOGDIR%\soak_report_24h_stderr.log"
echo ==== %DATE% %TIME% ====>>"%OUT%"
echo ==== %DATE% %TIME% ====>>"%ERR%"

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%PS%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SCRIPT_DIR%soak_report_24h_v1.ps1" -Repo "%REPO%" 1>>"%OUT%" 2>>"%ERR%"

set "RC=%ERRORLEVEL%"
echo EXITCODE=%RC%>>"%OUT%"

popd >nul
exit /b %RC%
