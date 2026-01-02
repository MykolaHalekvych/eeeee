@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%\..") do set "REPO=%%~fI"
pushd "%REPO%" >nul

set "LOGDIR=%REPO%\args\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "OUT=%LOGDIR%\ibkr_tap_once_stdout.log"
set "ERR=%LOGDIR%\ibkr_tap_once_stderr.log"

echo ==== %DATE% %TIME% ====>>"%OUT%"
echo ==== %DATE% %TIME% ====>>"%ERR%"

"%PS%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SCRIPT_DIR%ibkr_tap_once_sys.ps1" 1>>"%OUT%" 2>>"%ERR%"
set "RC=%ERRORLEVEL%"

echo EXITCODE=%RC%>>"%OUT%"
popd >nul
exit /b %RC%
