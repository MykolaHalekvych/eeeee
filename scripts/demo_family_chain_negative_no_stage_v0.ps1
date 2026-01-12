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

  $run_id = "NEG_NO_STAGE_{0}_{1}" -f (UtcNowId), (RandHex 4)
  $out_dir = Join-Path $repo ("args\data\smoke\family_chain_negative_no_stage_v0\{0}" -f $run_id)
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
    -SkipVaultStage "YES" 2> $stderr_path
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
  } catch { $parsed_ok = $false }

  $rc_norm = $rc
  if (($rc_norm -ne 0) -and ($rc_norm -ne 1) -and ($rc_norm -ne 2)) { $rc_norm = $RC_INFRA }

  # Expectations:
  # - vault_export must fail (rc=1 or 2) because staged run is absent
  # - guardian/governor must be rc=0
  # - only failing step should be vault_export
  $export_rc = $null
  $nonzero_steps = New-Object System.Collections.Generic.List[string]
  $guardian_rc = $null
  $governor_rc = $null

  if ($parsed_ok) {
    foreach ($s in $chain_obj.steps) {
      if ($s.step -eq "guardian_check") { $guardian_rc = $s.rc }
      if ($s.step -eq "governor_check") { $governor_rc = $s.rc }
      if ($s.step -eq "vault_export")   { $export_rc  = $s.rc }
      if ($s.rc -ne 0) { $nonzero_steps.Add([string]$s.step) | Out-Null }
    }
  }

  $test_ok = $true
  $why = "OK"

  if (-not $parsed_ok) {
    $test_ok = $false
    $why = "CHAIN_STDOUT_NOT_JSON"
  }
  elseif (($guardian_rc -ne 0) -or ($governor_rc -ne 0)) {
    $test_ok = $false
    $why = "EXPECTED_GUARDIAN_GOVERNOR_RC_0_GOT_guardian={0}_governor={1}" -f $guardian_rc, $governor_rc
  }
  elseif (($null -eq $export_rc) -or (($export_rc -ne 1) -and ($export_rc -ne 2))) {
    $test_ok = $false
    $why = "EXPECTED_VAULT_EXPORT_NONZERO_GOT_{0}" -f $export_rc
  }
  elseif (($rc_norm -ne 1) -and ($rc_norm -ne 2)) {
    $test_ok = $false
    $why = "EXPECTED_CHAIN_NONZERO_GOT_{0}" -f $rc_norm
  }
  elseif (($nonzero_steps.Count -ne 1) -or ($nonzero_steps[0] -ne "vault_export")) {
    $test_ok = $false
    $why = "EXPECTED_ONLY_VAULT_EXPORT_TO_FAIL_GOT_{0}" -f (($nonzero_steps -join ","))
  }

  $exit = $(if ($test_ok) { $RC_OK } elseif ($rc_norm -eq $RC_INFRA) { $RC_INFRA } else { $RC_FAIL })

  $out = [ordered]@{
    schema        = "demo_family_chain_negative_no_stage_v0"
    ts_utc        = (UtcNowIso)
    ok            = ($exit -eq 0)
    exit_code     = $exit
    repo          = $repo
    run_id        = $run_id
    out_dir       = $out_dir
    target_run_id = $TargetRunId
    config_used   = $cfg_abs
    expected      = [ordered]@{ failing_step = "vault_export"; vault_export_rc = "NONZERO(1|2)" }
    observed      = [ordered]@{
      chain_rc     = $rc_norm
      chain_rc_raw = $rc
      parsed_ok    = $parsed_ok
      guardian_rc  = $(if ($null -eq $guardian_rc) { "" } else { [int]$guardian_rc })
      governor_rc  = $(if ($null -eq $governor_rc) { "" } else { [int]$governor_rc })
      vault_export_rc = $(if ($null -eq $export_rc) { "" } else { [int]$export_rc })
      nonzero_steps = ($nonzero_steps -join ",")
      stdout_path  = $stdout_path
      stderr_path  = $stderr_path
      chain_run_id  = $($chain_obj.run_id)
      chain_run_dir = $($chain_obj.run_dir)
      chain_evidence_dir = $($chain_obj.evidence_dir)
    }
    reason = $why
  }

  ($out | ConvertTo-Json -Compress -Depth 10)
  exit $exit
}
catch {
  $repo = (Get-Location).Path
  $out = [ordered]@{
    schema    = "demo_family_chain_negative_no_stage_v0"
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