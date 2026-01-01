@echo off
setlocal
set REPO=C:\Users\mukol\ARGS-Core-v1
set PYEXE=C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe

if not exist "%REPO%\args\data" mkdir "%REPO%\args\data"
if not exist "%REPO%\args\logs" mkdir "%REPO%\args\logs"

echo %DATE% %TIME% AUTOLOOP REPO=%REPO%>> "%REPO%\args\data\scheduler_heartbeat.txt"
echo CD_before=%CD%>> "%REPO%\args\logs\scheduler_autoloop.log"

cd /d "%REPO%"
echo CD_after=%CD%>> "%REPO%\args\logs\scheduler_autoloop.log"

"%PYEXE%" -m args.ops.auto_loop_v1 --once >> "%REPO%\args\logs\scheduler_autoloop.log" 2>&1

exit /b %ERRORLEVEL%
