@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1

REM 0) Prepare log
set LOG=args\logs\scheduler_offline_nightly.log
echo [START] %DATE% %TIME% > %LOG%

REM 1) Build dataset from latest runs
py -3.11 -m args.offline.dataset_builder_v0 --latest 50 >> %LOG% 2>&1
if errorlevel 1 (
  echo [FAIL] dataset_builder rc=%ERRORLEVEL% >> %LOG%
  exit /b 2
)

REM 2) Train stub (produces model_<id>)
py -3.11 -m args.offline.train_stub_v0 >> %LOG% 2>&1
if errorlevel 1 (
  echo [FAIL] train_stub rc=%ERRORLEVEL% >> %LOG%
  exit /b 2
)

REM 3) Find latest model dir (best-effort: rely on train_stub stdout JSON)
REM    We parse last JSON line in log containing "model_dir"
for /f "usebackq delims=" %%L in (`powershell -NoProfile -Command ^
  "$t=Get-Content '%LOG%' -Raw; $m=[regex]::Matches($t,'\{.*?\}','Singleline'); if($m.Count -gt 0){$j=$m[$m.Count-1].Value; try{(ConvertFrom-Json $j).model_dir}catch{''}} else {''}"`) do set MODEL_DIR=%%L

if "%MODEL_DIR%"=="" (
  echo [FAIL] could not parse model_dir from train_stub output >> %LOG%
  exit /b 2
)

echo [INFO] model_dir=%MODEL_DIR% >> %LOG%

REM 4) Eval gate (PASS=0, FAIL=2)
py -3.11 -m args.offline.eval_gate_v0 --model_dir "%MODEL_DIR%" >> %LOG% 2>&1
set EVAL_RC=%ERRORLEVEL%
echo [INFO] eval_rc=%EVAL_RC% >> %LOG%

REM 5) If PASS -> promote; else skip promote
if "%EVAL_RC%"=="0" (
  py -3.11 -m args.offline.model_registry_v0 --promote --model_dir "%MODEL_DIR%" >> %LOG% 2>&1
  if errorlevel 1 (
    echo [FAIL] promote failed rc=%ERRORLEVEL% >> %LOG%
    exit /b 2
  )
) else (
  echo [INFO] eval not PASS; promote skipped >> %LOG%
)

REM 6) Verify audit & evidence pointers (must pass)
py -3.11 -m args.offline.model_registry_v0 --verify-audit >> %LOG% 2>&1
if errorlevel 1 (
  echo [FAIL] verify-audit rc=%ERRORLEVEL% >> %LOG%
  exit /b 2
)

py -3.11 -m args.offline.evidence_history_v0 verify --latest_pointer args\offline\evidence\eval_gate\latest.json >> %LOG% 2>&1
if errorlevel 1 (
  echo [FAIL] evidence verify rc=%ERRORLEVEL% >> %LOG%
  exit /b 2
)

echo [OK] nightly pipeline done >> %LOG%
exit /b 0
