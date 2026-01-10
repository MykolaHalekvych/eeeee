# Family Suite + Soak v1 (golden)

Golden soak run_id: **20260110T220642Z_ff35ffd4**
Example suite run_id (iter10): **20260110T225901Z_679dcf51**

## Run suite once
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_family_test_suite_v1.ps1

Artifacts:
- args\data\runs\<suite_run_id>\{events.jsonl, final_report.json, evidence\...}

## Run soak strict (N=10)
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_family_soak_strict_v1.ps1 -N 10

Expected:
- stdout: exactly 1 JSON
- exit_code: 0
- summary.passed = 10
- contract_gate_failed = 0; contract_gate_infra = 0

## Monitor progress (separate shell)
$soak="<RUN_ID>"
Get-Content ".\args\data\runs\$soak\events.jsonl" -Tail 20
