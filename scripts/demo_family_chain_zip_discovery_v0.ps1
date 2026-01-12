param(
  [Parameter(Mandatory=$true)][string]$TargetRunId,
  [Parameter(Mandatory=$false)][string]$Config = "manifests\family_chain\family_chain_config_v0.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) }

function RandHex([int]$n) {
  $bytes = New-Object byte[] $n
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  return ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
}

function WriteUtf8NoBom([string]$p, [string]$s) {
  $enc = New-Object System.Text.UTF8Encoding $false
  [IO.File]::WriteAllText($p, $s, $enc)
}

try {
  $repo = (Get-Location).Path
  if (-not (Test-Path (Join-Path $repo ".args_engine_repo"))) { throw "WRONG_REPO" }

  $run_id  = "ZIP_DISCOVERY_{0}_{1}" -f (UtcNowId), (RandHex 4)
  $out_dir = Join-Path $repo ("args\data\smoke\family_chain_zip_discovery_v0\{0}" -f $run_id)
  New-Item -ItemType Directory -Force -Path $out_dir | Out-Null

  $chain_script = Join-Path $repo "scripts\run_family_governance_chain_v0.ps1"
  if (-not (Test-Path $chain_script)) { throw "CHAIN_SCRIPT_NOT_FOUND" }

  $cfg = $Config
  $cfg_abs = $null
  try { $cfg_abs = (Resolve-Path $cfg).Path } catch { $cfg_abs = $cfg }

  $stdout_path = Join-Path $out_dir "chain.stdout.txt"
  $stderr_path = Join-Path $out_dir "chain.stderr.txt"

  $stdout = & powershell -NoProfile -ExecutionPolicy Bypass -File $chain_script `
    -TargetRunId $TargetRunId `
    -Config $cfg_abs `
    -ForceZipDiscovery "YES" 2> $stderr_path
  $rc = $LASTEXITCODE

  if ($null -eq $stdout) { $stdout = "" }
  if ($stdout -is [System.Array]) { $stdout = ($stdout -join "`n") }
  WriteUtf8NoBom $stdout_path ([string]$stdout)

  # Parse chain JSON
  $parsed_ok = $false
  $chain_obj = $null
  try {
    if ([string]::IsNullOrWhiteSpace([string]$stdout)) { throw "EMPTY_STDOUT" }
    $chain_obj = $stdout | ConvertFrom-Json -ErrorAction Stop
    $parsed_ok = $true
  } catch { $parsed_ok = $false }

  # Normalize rc
  $rc_norm = $rc
  if (($rc_norm -ne 0) -and ($rc_norm -ne 1) -and ($rc_norm -ne 2)) { $rc_norm = $RC_INFRA }

  # Best-effort: extract expected zip path from vault_export step
  $zip_path = ""
  if ($parsed_ok) {
    foreach ($s in $chain_obj.steps) {
      if ($s.step -eq "vault_export" -and $s.expected_zip_path) { $zip_path = [string]$s.expected_zip_path }
    }
  }

  $zip_exists = $false
  if (-not [string]::IsNullOrWhiteSpace($zip_path)) {
    if (Test-Path -LiteralPath $zip_path -PathType Leaf) { $zip_exists = $true }
  }

  $test_ok = $true
  $why = "OK"

  if (-not $parsed_ok) {
    $test_ok = $false
    $why = "CHAIN_STDOUT_NOT_JSON"
  }
  elseif ($rc_norm -ne 0) {
    $test_ok = $false
    $why = "EXPECTED_CHAIN_RC_0_GOT_{0}" -f $rc_norm
  }
  elseif (-not $zip_exists) {
    $test_ok = $false
    $why = "EXPECTED_EVIDENCE_ZIP_TO_EXIST"
  }

  $exit = $RC_FAIL
  if (-not $parsed_ok) { $exit = $RC_INFRA }
  elseif ($test_ok) { $exit = $RC_OK }
  else { $exit = $RC_FAIL }

  $out = [ordered]@{
    schema        = "demo_family_chain_zip_discovery_v0"
    ts_utc        = (UtcNowIso)
    ok            = ($exit -eq 0)
    exit_code     = $exit
    repo          = $repo
    run_id        = $run_id
    out_dir       = $out_dir
    target_run_id = $TargetRunId
    config_used   = $cfg_abs
    expected      = [ordered]@{ chain_rc=0; force_zip_discovery="YES"; evidence_zip_exists=$true }
    observed      = [ordered]@{
      chain_rc     = $rc_norm
      chain_rc_raw = $rc
      parsed_ok    = $parsed_ok
      zip_path     = $zip_path
      zip_exists   = $zip_exists
      stdout_path  = $stdout_path
      stderr_path  = $stderr_path
      chain_run_id  = $(if ($parsed_ok -and ($chain_obj.PSObject.Properties.Name -contains "run_id")) { [string]$chain_obj.run_id } else { "" })
      chain_reason  = $(if ($parsed_ok -and ($chain_obj.PSObject.Properties.Name -contains "reason")) { [string]$chain_obj.reason } else { "" })
    }
    reason = $why
  }

  ($out | ConvertTo-Json -Compress -Depth 10)
  exit $exit
}
catch {
  $repo = (Get-Location).Path
  $out = [ordered]@{
    schema    = "demo_family_chain_zip_discovery_v0"
    ts_utc    = (UtcNowIso)
    ok        = $false
    exit_code = $RC_INFRA
    repo      = $repo
    error     = [ordered]@{
      kind    = "exception"
      message = $_.Exception.Message
    }
  }
  ($out | ConvertTo-Json -Compress -Depth 6)
  exit $RC_INFRA
}
