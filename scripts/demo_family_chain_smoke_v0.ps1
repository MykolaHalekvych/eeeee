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

  $run_id = "SMOKE_{0}_{1}" -f (UtcNowId), (RandHex 4)
  $out_dir = Join-Path $repo ("args\data\smoke\family_chain_smoke_v0\{0}" -f $run_id)
  New-Item -ItemType Directory -Force -Path $out_dir | Out-Null

  $chain_script = Join-Path $repo "scripts\run_family_governance_chain_v0.ps1"
  if (-not (Test-Path $chain_script)) { throw "CHAIN_SCRIPT_NOT_FOUND" }

  $cfg = $Config
  $cfg_abs = $null
  try { $cfg_abs = (Resolve-Path $cfg).Path } catch { $cfg_abs = $cfg }

  $stdout_path = Join-Path $out_dir "chain.stdout.txt"
  $stderr_path = Join-Path $out_dir "chain.stderr.txt"

  # Run chain in child PowerShell to:
  # - capture its single-JSON stdout (so this wrapper still prints exactly one JSON)
  # - preserve exit code (0/1/2)
  $stdout = & powershell -NoProfile -ExecutionPolicy Bypass -File $chain_script -TargetRunId $TargetRunId -Config $cfg_abs 2> $stderr_path
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

  # Normalize rc to 0/1/2
  $rc_norm = $rc
  if (($rc_norm -ne 0) -and ($rc_norm -ne 1) -and ($rc_norm -ne 2)) { $rc_norm = $RC_INFRA }

  $out = [ordered]@{
    schema        = "demo_family_chain_smoke_v0"
    ts_utc        = (UtcNowIso)
    ok            = ($rc_norm -eq 0)
    exit_code     = $rc_norm
    repo          = $repo
    run_id        = $run_id
    out_dir       = $out_dir
    target_run_id = $TargetRunId
    config_used   = $cfg_abs
    chain = [ordered]@{
      rc          = $rc_norm
      rc_raw      = $rc
      parsed_ok   = $parsed_ok
      stdout_path = $stdout_path
      stderr_path = $stderr_path
      chain_run_id     = $(if ($parsed_ok) { $chain_obj.run_id } else { "" })
      chain_run_dir    = $(if ($parsed_ok) { $chain_obj.run_dir } else { "" })
      chain_evidence   = $(if ($parsed_ok) { $chain_obj.evidence_dir } else { "" })
      chain_zip_path   = $(if ($parsed_ok) {
        # best-effort: pick expected_zip_path from steps[vault_export] if present
        $z = ""
        foreach ($s in $chain_obj.steps) {
          if ($s.step -eq "vault_export" -and $s.expected_zip_path) { $z = $s.expected_zip_path }
        }
        $z
      } else { "" })
    }
  }

  ($out | ConvertTo-Json -Compress -Depth 10)
  exit $rc_norm
}
catch {
  $repo = (Get-Location).Path
  $out = [ordered]@{
    schema    = "demo_family_chain_smoke_v0"
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