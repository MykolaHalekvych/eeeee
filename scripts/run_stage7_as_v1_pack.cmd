@echo off
set REPO=C:\Users\mukol\ARGS-Core-v1

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass ^
  -File "%REPO%\scripts\stage7_as_v1_pack.ps1" ^
  -Repo "%REPO%" ^
  >> "%REPO%\args\logs\stage7_as_v1_pack_stdout.log" ^
  2>> "%REPO%\args\logs\stage7_as_v1_pack_stderr.log"

exit /b %ERRORLEVEL%
