@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\scripts\housekeeping_v1.ps1
exit /b %ERRORLEVEL%
