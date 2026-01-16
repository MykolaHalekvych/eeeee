param(
  [Parameter(Mandatory=$true)][string]$PythonExe,
  [string]$Repo = ".",
  [string]$ArtifactRoot = "C:\Users\mukol\ARGS_ARTIFACTS\Engine-Foundry-v0",
  [string]$RunAcceptance = "NO"
)

$ErrorActionPreference = "Stop"

function New-RunId {
  param([string]$Prefix)
  $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  $hex = -join (1..8 | ForEach-Object { "{0:x}" -f (Get-Random -Max 16) })
  return "$Prefix$ts" + "_" + $hex
}

function Write-JsonLine {
  param([object]$Obj)
  $json = $Obj | ConvertTo-Json -Compress -Depth 50
  Write-Output $json
}

function Save-Text {
  param([string]$Path, [string]$Text)
  $dir = Split-Path -Parent $Path
  if ($dir -and !(Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  Set-Content -Encoding UTF8 -Path $Path -Value $Text
}

function Save-JsonPretty {
  param([string]$Path, [object]$Obj)
  $dir = Split-Path -Parent $Path
  if ($dir -and !(Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  ($Obj | ConvertTo-Json -Depth 50) | Set-Content -Encoding UTF8 -Path $Path
}

$ts_utc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$run_id = New-RunId -Prefix "LLM_PROOF_"
$out_dir = Join-Path $ArtifactRoot ("llm_proposal_layer_v0_proof_pack_v1\" + $run_id)

$summary = @{
  schema = "llm_proposal_layer_v0_proof_pack_v1"
  ts_utc = $ts_utc
  ok = $false
  exit_code = 2
  reason_code = "INFRA_UNKNOWN"
  child_reason_code = "INFRA_UNKNOWN"
  repo = (Resolve-Path $Repo).Path
  run_id = $run_id
  out_dir = $out_dir
  results = @()
}

try {
  if ($RunAcceptance -ne "YES") {
    $summary.exit_code = 2
    $summary.reason_code = "INFRA_RUN_ACCEPTANCE_REQUIRED"
    $summary.child_reason_code = "RunAcceptance!=YES"
    Save-JsonPretty -Path (Join-Path $out_dir "summary.json") -Obj $summary
    Write-JsonLine -Obj $summary
    exit 2
  }

  if (!(Test-Path $PythonExe)) {
    $summary.exit_code = 2
    $summary.reason_code = "INFRA_PYTHON_EXE_NOT_FOUND"
    $summary.child_reason_code = "PythonExe_not_found"
    $summary.results += @{ name="preflight"; ok=$false; exit_code=2; reason_code=$summary.reason_code; child_reason_code=$summary.child_reason_code; python_exe=$PythonExe }
    Save-JsonPretty -Path (Join-Path $out_dir "summary.json") -Obj $summary
    Write-JsonLine -Obj $summary
    exit 2
  }

  New-Item -ItemType Directory -Force -Path $out_dir | Out-Null

  $validator = Join-Path $Repo "args\llm\validate_llm_proposal_v0.py"
  $stub      = Join-Path $Repo "args\llm\llm_stub_v0.py"

  # ---- PASS ----
  $pass_dir = Join-Path $out_dir "step_00_PASS"
  New-Item -ItemType Directory -Force -Path $pass_dir | Out-Null

  $jr_path = Join-Path $pass_dir "job_request_pass.json"
  $jr = Get-Content -Raw -Path (Join-Path $Repo "args\llm\contracts\llm_job_request_v0.json") | ConvertFrom-Json
  $jr.run_id = "PASS_" + $run_id
  $jr | ConvertTo-Json -Depth 50 | Set-Content -Encoding UTF8 -Path $jr_path

  $res_path = Join-Path $pass_dir "job_result_pass.json"

  $stub_stdout = Join-Path $pass_dir "stub.stdout.json"
  $stub_stderr = Join-Path $pass_dir "stub.stderr.txt"
  $stub_json = & $PythonExe $stub --JobRequest $jr_path --OutResult $res_path --OutDir (Join-Path $pass_dir "stub_out") 2> $stub_stderr
  Save-Text -Path $stub_stdout -Text ($stub_json | Out-String)
  $stub_ec = $LASTEXITCODE

  $val_stdout = Join-Path $pass_dir "validator.stdout.json"
  $val_stderr = Join-Path $pass_dir "validator.stderr.txt"
  $val_json = & $PythonExe $validator --JobRequest $jr_path --JobResult $res_path --OutDir (Join-Path $pass_dir "val_out") 2> $val_stderr
  Save-Text -Path $val_stdout -Text ($val_json | Out-String)
  $val_ec = $LASTEXITCODE

  $val_obj = $val_json | ConvertFrom-Json
  $pass_ok = ($stub_ec -eq 0) -and ($val_ec -eq 0) -and ($val_obj.ok -eq $true)

  $summary.results += @{
    name="PASS_valid"
    ok=$pass_ok
    stub_exit_code=$stub_ec
    validator_exit_code=$val_ec
    validator_reason_code=$val_obj.reason_code
    validator_child_reason_code=$val_obj.child_reason_code
    out_dir=$pass_dir
  }

  # ---- NEG invalid JSON ----
  $neg_dir = Join-Path $out_dir "step_01_NEG_JSON_INVALID"
  New-Item -ItemType Directory -Force -Path $neg_dir | Out-Null

  $jr2_path = Join-Path $neg_dir "job_request_neg.json"
  $jr | ConvertTo-Json -Depth 50 | Set-Content -Encoding UTF8 -Path $jr2_path

  $bad_res = Join-Path $neg_dir "job_result_invalid.json"
  Set-Content -Encoding UTF8 -Path $bad_res -Value "{"

  $neg_stdout = Join-Path $neg_dir "validator.stdout.json"
  $neg_stderr = Join-Path $neg_dir "validator.stderr.txt"
  $neg_json = & $PythonExe $validator --JobRequest $jr2_path --JobResult $bad_res --OutDir (Join-Path $neg_dir "val_out") 2> $neg_stderr
  Save-Text -Path $neg_stdout -Text ($neg_json | Out-String)
  $neg_ec = $LASTEXITCODE
  $neg_obj = $neg_json | ConvertFrom-Json

  $neg_ok = ($neg_ec -eq 1) -and ($neg_obj.reason_code -eq "FAIL_CONTRACT_JSON_INVALID")
  $summary.results += @{
    name="NEG_invalid_json"
    ok=$neg_ok
    validator_exit_code=$neg_ec
    validator_reason_code=$neg_obj.reason_code
    validator_child_reason_code=$neg_obj.child_reason_code
    out_dir=$neg_dir
  }

  # ---- DENY patch outside allowlist ----
  $deny_dir = Join-Path $out_dir "step_02_DENY_ALLOWLIST"
  New-Item -ItemType Directory -Force -Path $deny_dir | Out-Null

  $jr3_path = Join-Path $deny_dir "job_request_deny.json"
  $jr | ConvertTo-Json -Depth 50 | Set-Content -Encoding UTF8 -Path $jr3_path

  $deny_res = Join-Path $deny_dir "job_result_deny.json"
  @"
{
  "schema": "llm_job_result_v0",
  "ok": true,
  "proposals": [
    { "kind": "TEXT", "text": "DENY test: patch outside allowlist." }
  ],
  "patch_payload": {
    "format": "patch_payload_v0",
    "changes": [
      { "path": "args/llm/fixtures/forbidden_outside_allowlist.txt", "op": "write", "content": "X\n" }
    ]
  },
  "diagnostics": { "tokens_in": 0, "tokens_out": 0, "notes": "deny_test" },
  "model_id": "stub_v0"
}
"@ | Set-Content -Encoding UTF8 -Path $deny_res

  $deny_stdout = Join-Path $deny_dir "validator.stdout.json"
  $deny_stderr = Join-Path $deny_dir "validator.stderr.txt"
  $deny_json = & $PythonExe $validator --JobRequest $jr3_path --JobResult $deny_res --OutDir (Join-Path $deny_dir "val_out") 2> $deny_stderr
  Save-Text -Path $deny_stdout -Text ($deny_json | Out-String)
  $deny_ec = $LASTEXITCODE
  $deny_obj = $deny_json | ConvertFrom-Json

  $deny_ok = ($deny_ec -eq 1) -and ($deny_obj.reason_code -eq "FAIL_ALLOWLIST_VIOLATION")
  $summary.results += @{
    name="DENY_allowlist_violation"
    ok=$deny_ok
    validator_exit_code=$deny_ec
    validator_reason_code=$deny_obj.reason_code
    validator_child_reason_code=$deny_obj.child_reason_code
    out_dir=$deny_dir
  }

  # ---- INFRA missing refs ----
  $infra_dir = Join-Path $out_dir "step_03_INFRA_MISSING_REFS"
  New-Item -ItemType Directory -Force -Path $infra_dir | Out-Null

  $jr4_path = Join-Path $infra_dir "job_request_infra.json"
  $jr_infra = $jr.PSObject.Copy()
  $jr_infra.allowed_paths_ref = "args/llm/fixtures/DOES_NOT_EXIST.json"
  $jr_infra | ConvertTo-Json -Depth 50 | Set-Content -Encoding UTF8 -Path $jr4_path

  $res4_path = Join-Path $infra_dir "job_result_ok.json"
  Get-Content -Raw -Path (Join-Path $Repo "args\llm\contracts\llm_job_result_v0.json") | Set-Content -Encoding UTF8 -Path $res4_path

  $infra_stdout = Join-Path $infra_dir "validator.stdout.json"
  $infra_stderr = Join-Path $infra_dir "validator.stderr.txt"
  $infra_json = & $PythonExe $validator --JobRequest $jr4_path --JobResult $res4_path --OutDir (Join-Path $infra_dir "val_out") 2> $infra_stderr
  Save-Text -Path $infra_stdout -Text ($infra_json | Out-String)
  $infra_ec = $LASTEXITCODE
  $infra_obj = $infra_json | ConvertFrom-Json

  $infra_ok = ($infra_ec -eq 2) -and ($infra_obj.reason_code -eq "INFRA_MISSING_INPUT")
  $summary.results += @{
    name="INFRA_missing_file_refs"
    ok=$infra_ok
    validator_exit_code=$infra_ec
    validator_reason_code=$infra_obj.reason_code
    validator_child_reason_code=$infra_obj.child_reason_code
    out_dir=$infra_dir
  }

  $all_ok = $true
  foreach ($r in $summary.results) { if (-not $r.ok) { $all_ok = $false } }

  if ($all_ok) {
    $summary.ok = $true
    $summary.exit_code = 0
    $summary.reason_code = "OK"
    $summary.child_reason_code = "OK"
  } else {
    $summary.ok = $false
    $summary.exit_code = 1
    $summary.reason_code = "FAIL_SUBTEST"
    $summary.child_reason_code = "one_or_more_subtests_failed"
  }

} catch {
  $summary.ok = $false
  $summary.exit_code = 2
  $summary.reason_code = "INFRA_EXCEPTION"
  $summary.child_reason_code = "INFRA_EXCEPTION"
  $summary.results += @{ name="exception"; ok=$false; exit_code=2; error=($_ | Out-String) }
}

Save-JsonPretty -Path (Join-Path $out_dir "summary.json") -Obj $summary
Write-JsonLine -Obj $summary
exit $summary.exit_code
