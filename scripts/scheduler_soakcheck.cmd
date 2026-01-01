@echo off
setlocal
set REPO=C:\Users\mukol\ARGS-Core-v1
set PYEXE=C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe

if not exist "%REPO%\args\data" mkdir "%REPO%\args\data"
if not exist "%REPO%\args\logs" mkdir "%REPO%\args\logs"

echo %DATE% %TIME% SOAK REPO=%REPO%>> "%REPO%\args\data\scheduler_heartbeat.txt"
cd /d "%REPO%"

"%PYEXE%" -m args.ops.soak_check_v0 --last 50 --require_run_change >> "%REPO%\args\logs\scheduler_soakcheck.log" 2>&1

exit /b %ERRORLEVEL%
