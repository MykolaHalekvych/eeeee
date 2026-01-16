param(
  # --- Control token inputs ---
  [string]$ControlTokenPath = "",
  [string]$ControlRunId = "",
  [string]$ControlJobType = "FOUNDRY_BUILD_WINDOW_V1",

  # --- What to invoke if token OK ---
  [string]$InnerScript = "",

  # JSON array of args for inner script, e.g. ["-KitId","...", ...]
  [string]$InnerArgsJson = "[]"
)

$ErrorActionPreference = "Stop"
$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")

function EmitAndExit([int]$code, [string]$reason, [string]$detail) {
  $o = @{
    schema      = "foundry_control_guard_v0"
    ts_utc      = $ts
    ok          = ($code -eq 0)
    exit_code   = $code
    reason_code = $reason
  }
  if ($detail) { $o.detail = $detail }
  Write-Output ($o | ConvertTo-Json -Compress -Depth 20)
  exit $code
}

# Expected job type
if ([string]::IsNullOrWhiteSpace($ControlJobType)) { $ControlJobType = "FOUNDRY_BUILD_WINDOW_V1" }
if ($ControlJobType -ne "FOUNDRY_BUILD_WINDOW_V1") {
  EmitAndExit 1 "CONTROL_TOKEN.JOB_TYPE_UNEXPECTED" ("expected FOUNDRY_BUILD_WINDOW_V1, got: " + $ControlJobType)
}

# Token presence
if ([string]::IsNullOrWhiteSpace($ControlTokenPath)) { EmitAndExit 1 "CONTROL_TOKEN.MISSING" "ControlTokenPath empty" }
if ([string]::IsNullOrWhiteSpace($ControlRunId))    { EmitAndExit 1 "CONTROL_TOKEN.RUN_ID_MISSING" "ControlRunId empty" }
if (-not (Test-Path -LiteralPath $ControlTokenPath)) { EmitAndExit 1 "CONTROL_TOKEN.MISSING" ("not found: " + $ControlTokenPath) }

# Token parse & verify
$tok = $null
try { $tok = Get-Content -LiteralPath $ControlTokenPath -Raw -ErrorAction Stop | ConvertFrom-Json }
catch { EmitAndExit 1 "CONTROL_TOKEN.BAD_JSON" $_.Exception.Message }

if ($tok.schema -ne "control_token_v0") { EmitAndExit 1 "CONTROL_TOKEN.SCHEMA_MISMATCH" ("schema=" + [string]$tok.schema) }
if ($tok.run_id -ne $ControlRunId)      { EmitAndExit 1 "CONTROL_TOKEN.RUN_ID_MISMATCH" ("token.run_id=" + [string]$tok.run_id + "; expected=" + $ControlRunId) }
if ($tok.job_type -ne $ControlJobType)  { EmitAndExit 1 "CONTROL_TOKEN.JOB_TYPE_MISMATCH" ("token.job_type=" + [string]$tok.job_type + "; expected=" + $ControlJobType) }
if (-not ([string]$tok.nonce -match '^[a-f0-9]{16,64}$')) { EmitAndExit 1 "CONTROL_TOKEN.NONCE_INVALID" ("nonce=" + [string]$tok.nonce) }

# Resolve inner script default
if ([string]::IsNullOrWhiteSpace($InnerScript)) {
  $InnerScript = Join-Path $PSScriptRoot "factory_build_window_v1.ps1"
}
if (-not (Test-Path -LiteralPath $InnerScript)) {
  EmitAndExit 2 "INNER_SCRIPT.MISSING" ("inner not found: " + $InnerScript)
}

# Parse args JSON
$innerArgs = @()
try {
  $parsed = $InnerArgsJson | ConvertFrom-Json
  if ($null -eq $parsed) { $innerArgs = @() }
  elseif ($parsed -is [System.Array]) {
    foreach ($x in $parsed) { $innerArgs += [string]$x }
  } else {
    EmitAndExit 2 "INNER_ARGS_JSON.INVALID" "Expected JSON array of strings"
  }
} catch {
  EmitAndExit 2 "INNER_ARGS_JSON.BAD_JSON" $_.Exception.Message
}

# IMPORTANT: pass token params into inner as well (your inner expects them)
if (-not ($innerArgs -contains "-ControlTokenPath")) { $innerArgs = @("-ControlTokenPath", $ControlTokenPath) + $innerArgs }
if (-not ($innerArgs -contains "-ControlRunId"))     { $innerArgs = @("-ControlRunId", $ControlRunId) + $innerArgs }
if (-not ($innerArgs -contains "-ControlJobType"))   { $innerArgs = @("-ControlJobType", $ControlJobType) + $innerArgs }

# Invoke inner
$out = & powershell -NoProfile -ExecutionPolicy Bypass -File $InnerScript @innerArgs
$rc = $LASTEXITCODE

Write-Output $out
exit $rc
