@echo off
setlocal
cd /d %~dp0\..
REM Usage: run_verify_release_v0.cmd <release_id>
if "%~1"=="" (
  echo ERROR: provide release_id
  exit /b 2
)
py -3.11 -m args.foundry.release_verify_v0 --release-id %~1
exit /b %errorlevel%
