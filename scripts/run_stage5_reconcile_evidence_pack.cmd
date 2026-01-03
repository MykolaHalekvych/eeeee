@echo off
set REPO=C:\Users\mukol\ARGS-Core-v1

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass ^
  -File "%REPO%\scripts\stage5_reconcile_evidence_pack_v2.ps1" ^
  -Repo "%REPO%" ^
  -WriteCompatLatest ^
  >> "%REPO%\args\logs\stage5_reconcile_v2_stdout.log" ^
  2>> "%REPO%\args\logs\stage5_reconcile_v2_stderr.log"

exit /b %ERRORLEVEL%
