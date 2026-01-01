@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\Users\mukol\ARGS-Core-v1\scripts\auto_loop_5m_with_rotate.ps1 -Cycles 1 -IntervalSec 0 > args\logs\scheduler_autoloop_with_rotate.log 2>&1

exit /b %ERRORLEVEL%
