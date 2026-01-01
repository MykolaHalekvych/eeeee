@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1

set LOG=args\logs\scheduler_offline_nightly.log
echo [START] %DATE% %TIME% > %LOG%

REM 1) dataset
py -3.11 -m args.offline.dataset_builder_v0 --latest 50 >> %LOG% 2>&1
if errorlevel 1 goto FAIL_INFRA

REM 2) train
py -3.11 -m args.offline.train_stub_v0 >> %LOG% 2>&1
if errorlevel 1 goto FAIL_INFRA

REM 3) parse model_dir from last JSON in log
for /f "usebackq delims=" %%L in (`powershell -NoProfile -Command ^
  "$t=Get-Content '%LOG%' -Raw; $m=[regex]::Matches($t,'\{.*?\}','Singleline'); if($m.Count -gt 0){$j=$m[$m.Count-1].Value; try{(ConvertFrom-Json $j).model_dir}catch{''}} else {''}"`) do set MODEL_DIR=%%L

if "%MODEL_DIR%"=="" goto FAIL_INFRA
echo [INFO] model_dir=%MODEL_DIR% >> %LOG%

if not exist "%MODEL_DIR%\model_manifest.json" goto FAIL_INFRA

REM 4) eval (0 PASS, 2 FAIL)
py -3.11 -m args.offline.eval_gate_v0 --model "%MODEL_DIR%" >> %LOG% 2>&1
set EVAL_RC=%ERRORLEVEL%
echo [INFO] eval_rc=%EVAL_RC% >> %LOG%

REM 5) promote only if eval PASS
if "%EVAL_RC%"=="0" goto DO_PROMOTE
goto SKIP_PROMOTE

:DO_PROMOTE
py -3.11 -m args.offline.model_registry_v0 --promote --model_dir "%MODEL_DIR%" >> %LOG% 2>&1
if errorlevel 1 goto FAIL_INFRA
goto AFTER_PROMOTE

:SKIP_PROMOTE
echo [WARN] eval FAIL (quality gate); promote skipped >> %LOG%
goto AFTER_PROMOTE

:AFTER_PROMOTE
REM 6) verify audit/evidence (must pass)
py -3.11 -m args.offline.model_registry_v0 --verify-audit >> %LOG% 2>&1
if errorlevel 1 goto FAIL_INFRA

py -3.11 -m args.offline.evidence_history_v0 verify --latest_pointer args\offline\evidence\eval_gate\latest.json >> %LOG% 2>&1
if errorlevel 1 goto FAIL_INFRA

REM Final exit policy: 0 if eval PASS else 1
if "%EVAL_RC%"=="0" goto OK_PASS
goto OK_WARN

:OK_PASS
echo [OK] nightly pipeline done (eval PASS) >> %LOG%
exit /b 0

:OK_WARN
echo [WARN] nightly pipeline done (eval FAIL) >> %LOG%
exit /b 1

:FAIL_INFRA
echo [FAIL] nightly pipeline infra error rc=%ERRORLEVEL% >> %LOG%
exit /b 2
