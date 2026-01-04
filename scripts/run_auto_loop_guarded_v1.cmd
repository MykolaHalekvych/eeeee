@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\scripts\auto_loop_5m_guarded_v1.ps1 -Cycles 1 -IntervalSec 0
exit /b %ERRORLEVEL%
