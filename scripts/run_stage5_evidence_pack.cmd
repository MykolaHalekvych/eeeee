@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%\..") do set "REPO=%%~fI"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

"%PS%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%SCRIPT_DIR%stage5_terminal_evidence_pack.ps1" -Repo "%REPO%" -TailLines 2000 -MinSecondsBetweenEvidence 30
exit /b %ERRORLEVEL%
