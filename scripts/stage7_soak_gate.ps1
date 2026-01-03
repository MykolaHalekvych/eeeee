[CmdletBinding()]
param(
  [string]$Repo = "",
  [int]$MaxTapAgeSec = 180,
  [int]$MaxEvidenceAgeSec = 900
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }

function _task_status([string]$tn) {
  $out = (schtasks /Query /TN "\$tn" /V /FO LIST) 2>$null
  $status = ($out | Select-String "^Status:" | ForEach-Object { ($_ -split ":\s+",2)[1].Trim() }) | Select-Object -First 1
  $last   = ($out | Select-String "^Last Result:" | ForEach-Object { ($_ -split ":\s+",2)[1].Trim() }) | Select-Object -First 1
  return [ordered]@{ task=$tn; status=$status; last_result=$last }
}

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) {
    $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
  } else {
    $Repo = (Resolve-Path -LiteralPath $Repo).Path
  }

  $tapHealth = Join-Path $Repo "args\data\ibkr_event_tap_health_v1.json"
  $evidenceBase = Join-Path $Repo "args\stage5_evidence"
  $outPath = Join-Path $Repo "args\data\ops_soak_gate.json"

  $issues = @()
  $warns  = @()

  # --- Tap health ---
  if (-not (Test-Path -LiteralPath $tapHealth)) {
    $issues += "tap_health_missing"
  } else {
    $fi = Get-Item -LiteralPath $tapHealth
    $ageTap = ([DateTime]::UtcNow - $fi.LastWriteTimeUtc).TotalSeconds
    if ($ageTap -gt $MaxTapAgeSec) { $issues += ("tap_health_stale_sec=" + [Math]::Round($ageTap,1)) }

    $h = Get-Content -LiteralPath $tapHealth -Raw | ConvertFrom-Json
    if (-not $h.ok) { $issues += "tap_health_ok=false" }
    if ($h.exit_code -ne 0) { $issues += ("tap_exit_code=" + $h.exit_code) }
  }

  # --- Evidence freshness ---
  if (-not (Test-Path -LiteralPath $evidenceBase)) {
    $issues += "evidence_dir_missing"
  } else {
    $latest = Get-ChildItem -LiteralPath $evidenceBase -Directory | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $latest) {
      $warns += "no_evidence_dirs"
    } else {
      $ageEv = ([DateTime]::UtcNow - $latest.LastWriteTimeUtc).TotalSeconds
      if ($ageEv -gt $MaxEvidenceAgeSec) { $warns += ("evidence_stale_sec=" + [Math]::Round($ageEv,1)) }
    }
  }
  # --- Terminal proof freshness ---
  $proofCursor = Join-Path $Repo "args\data\stage5_terminal_proof.cursor.json"
  if (-not (Test-Path -LiteralPath $proofCursor)) {
    $warns += "terminal_proof_cursor_missing"
  } else {
    $fiP = Get-Item -LiteralPath $proofCursor
    $ageP = ([DateTime]::UtcNow - $fiP.LastWriteTimeUtc).TotalSeconds
    if ($ageP -gt 21600) { $warns += ("terminal_proof_stale_sec=" + [Math]::Round($ageP,1)) }  # 6h
  }
  # --- Task status ---
  $t1 = _task_status "ARGS_IBKR_Tap_1m"
  $t2 = _task_status "ARGS_Stage5_Evidence_5m"

  $exit = 0
  $status = "OK"
  if ($issues.Count -gt 0) { $exit = 2; $status = "FAIL" }
  elseif ($warns.Count -gt 0) { $exit = 1; $status = "WARN" }

  $obj = [ordered]@{
    schema="ops_soak_gate_v1"
    ts_utc=_utc
    status=$status
    exit_code=$exit
    repo=$Repo
    max_tap_age_sec=$MaxTapAgeSec
    max_evidence_age_sec=$MaxEvidenceAgeSec
    issues=$issues
    warns=$warns
    tap_task=$t1
    evidence_task=$t2
    tap_health_path=$tapHealth
    evidence_base=$evidenceBase
  }

  ($obj | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $outPath -Encoding UTF8
  ($obj | ConvertTo-Json -Compress -Depth 8) | Write-Output
  exit $exit
}
catch {
  $obj = [ordered]@{
    schema="ops_soak_gate_v1"
    ts_utc=_utc
    status="FAIL"
    exit_code=2
    error=$_.Exception.Message
  }
  try {
    if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
    $outPath = Join-Path $Repo "args\data\ops_soak_gate.json"
    ($obj | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $outPath -Encoding UTF8
  } catch {}
  ($obj | ConvertTo-Json -Compress -Depth 6) | Write-Output
  exit 2
}

