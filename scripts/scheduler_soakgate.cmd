@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1

py -3.11 -m args.ops.soak_gate_v1 > args\logs\scheduler_soakgate.log 2>&1
set RC=%ERRORLEVEL%

py -3.11 -m args.ops.soak_history_v0 >> args\logs\scheduler_soakgate.log 2>&1

exit /b %RC%
