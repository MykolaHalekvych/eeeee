@echo off
setlocal
set REPO=C:\Users\mukol\ARGS-Core-v1
set PYEXE=C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe

cd /d "%REPO%"
"%PYEXE%" -m args.ops.auto_loop_v1 --once >> "%REPO%\args\logs\scheduler_autoloop_5m.log" 2>&1

exit /b %ERRORLEVEL%
