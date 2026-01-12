param(
  [Parameter(Mandatory=$true)][string]$TargetRunId,
  [Parameter(Mandatory=$false)][string]$Config = "manifests\family_chain\family_chain_config_v0.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exit codes (project standard)
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
  # Repo guard
  $repo = (Get-Location).Path
  if (-not (Test-Path (Join-Path $repo ".args_engine_repo"))) { throw "WRONG_REPO" }

  $run_id = "NEG_CORRUPT_ZIP_{0}_{1}" -f (UtcNowId), (RandHex 4)
  $out_dir = Join-Path $repo ("args\data\smoke\family_chain_negative_corrupt_zip_v0\{0}" -f $run_id)
  New-Item -ItemType Directory -Force -Path $out_dir | Out-Null

  $chain_script = Join-Path $repo "scripts\run_family_governance_chain_v0.ps1"
  if (-not (Test-Path $chain_script)) { throw "CHAIN_SCRIPT_NOT_FOUND" }

  $cfg = $Config
  $cfg_abs = $null
  try { $cfg_abs = (Resolve-Path $cfg).Path } catch { $cfg_abs = $cfg }

  $stdout_path = Join-Path $out_dir "chain.stdout.txt"
  $stderr_path = Join-Path $out_dir "chain.stderr.txt"

  # Run chain with chaos: corrupt zip before verify
  $stdout = & powershell -NoProfile -ExecutionPolicy Bypass -File $chain_script `
    -TargetRunId $TargetRunId `
    -Config $cfg_abs `
    -ChaosCorruptZipBeforeVerify "YES" 2> $stderr_path
  $rc = $LASTEXITCODE

  if ($null -eq $stdout) { $stdout = "" }
  if ($stdout -is [System.Array]) { $stdout = ($stdout -join "`n") }
  WriteUtf8NoBom $stdout_path ([string]$stdout)

  $parsed_ok = $false
  $chain_obj = $null
  try {
    if ([string]::IsNullOrWhiteSpace([string]$stdout)) { throw "EMPTY_STDOUT" }
    $chain_obj = $stdout | ConvertFrom-Json -ErrorAction Stop
    $parsed_ok = $true
  } catch {
    $parsed_ok = $false
  }

  # Normalize rc to 0/1/2 (infra if unexpected)
  $rc_norm = $rc
  if (($rc_norm -ne 0) -and ($rc_norm -ne 1) -and ($rc_norm -ne 2)) { $rc_norm = $RC_INFRA }

  # Expectations:
  # - chain must FAIL with rc=1 (verify should fail due to corrupted zip)
  # - vault_verify step rc should be 1 (best-effort check if parsed)
  $expect_chain_rc = 1
  $verify_rc = $null
  if ($parsed_ok) {
    foreach ($s in $chain_obj.steps) {
      if ($s.step -eq "vault_verify") { $verify_rc = $s.rc }
    }
  }

  $test_ok = $true
  $why = "OK"
  if ($rc_norm -ne $expect_chain_rc) {
    $test_ok = $false
    $why = "EXPECTED_CHAIN_RC_1_GOT_{0}" -f $rc_norm
  } elseif ($parsed_ok -and ($null -ne $verify_rc) -and ($verify_rc -ne 1)) {
    $test_ok = $false
    $why = "EXPECTED_VAULT_VERIFY_RC_1_GOT_{0}" -f $verify_rc
  }

  $exit = $(if ($test_ok) { $RC_OK } elseif ($rc_norm -eq $RC_INFRA) { $RC_INFRA } else { $RC_FAIL })

  $out = [ordered]@{
    schema        = "demo_family_chain_negative_corrupt_zip_v0"
    ts_utc        = (UtcNowIso)
    ok            = ($exit -eq 0)
    exit_code     = $exit
    repo          = $repo
    run_id        = $run_id
    out_dir       = $out_dir
    target_run_id = $TargetRunId
    config_used   = $cfg_abs
    expected      = [ordered]@{ chain_rc = 1; vault_verify_rc = 1 }
    observed      = [ordered]@{
      chain_rc     = $rc_norm
      chain_rc_raw = $rc
      parsed_ok    = $parsed_ok
      vault_verify_rc = $(if ($null -eq $verify_rc) { "" } else { [int]$verify_rc })
      stdout_path  = $stdout_path
      stderr_path  = $stderr_path
      chain_run_id  = $(if ($parsed_ok) { $chain_obj.run_id } else { "" })
      chain_run_dir = $(if ($parsed_ok) { $chain_obj.run_dir } else { "" })
      chain_evidence_dir = $(if ($parsed_ok) { $chain_obj.evidence_dir } else { "" })
    }
    reason = $why
  }

  ($out | ConvertTo-Json -Compress -Depth 10)
  exit $exit
}
catch {
  $repo = (Get-Location).Path
  $out = [ordered]@{
    schema    = "demo_family_chain_negative_corrupt_zip_v0"
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