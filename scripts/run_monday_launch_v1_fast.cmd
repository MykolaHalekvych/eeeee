@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\scripts\monday_launch_v1.ps1 -MaxAttempts 1 -SleepSec 0
exit /b %ERRORLEVEL%
