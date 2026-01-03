<# 
STAGE7_AS_V1_PACK_V2 (ops-grade)

- Preflight: require reconcile_evidence_latest_v2.json status=OK or WARN
  - OK/WARN  -> proceed to run AS v1
  - FAIL/UNKNOWN/missing/unreadable -> WARN exit 1 (do not run AS)
- Runs AS v1 (intents + FSM), no trading actions
- Uses Start-Process with WorkingDirectory=$Repo to avoid "No module named 'args'" under SYSTEM
- Writes:
    args\data\stage7_as_v1_pack_latest.json
    args\logs\stage7_as_v1_stdout.log / stage7_as_v1_stderr.log
- Exit codes: 0=OK, 1=WARN, 2=FAIL
#>

[CmdletBinding()]
param(
  [string]$Repo = "",
  [string]$Universe = "MHG",
  [int]$CooldownSec = 900
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }

function _pick_python() {
  $py311 = "C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe"
  if (Test-Path -LiteralPath $py311) { return [ordered]@{ exe=$py311; prefix=@() } }
  return [ordered]@{ exe="C:\Windows\py.exe"; prefix=@("-3.11") }
}

function _read_json_retry([string]$p, [int]$tries=6, [int]$sleepMs=80) {
  for ($i=0; $i -lt $tries; $i++) {
    if (Test-Path -LiteralPath $p) {
      try { return (Get-Content -LiteralPath $p -Raw | ConvertFrom-Json) } catch { Start-Sleep -Milliseconds $sleepMs; continue }
    }
    Start-Sleep -Milliseconds $sleepMs
  }
  return $null
}

function _write_json([string]$p, $obj, [int]$depth=12) {
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null
  ($obj | ConvertTo-Json -Depth $depth) | Set-Content -LiteralPath $p -Encoding UTF8
}

function _tail([string]$p, [int]$n=120) {
  if (-not (Test-Path -LiteralPath $p)) { return "" }
  try { return ((Get-Content -LiteralPath $p -Tail $n) -join "`n") } catch { return "" }
}

function _norm_status([object]$x) {
  if ($null -eq $x) { return "UNKNOWN" }
  $s = ("" + $x).Trim().ToUpper()
  if ([string]::IsNullOrWhiteSpace($s)) { return "UNKNOWN" }
  return $s
}

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
  else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }

  $reconcileV2 = Join-Path $Repo "args\data\reconcile_evidence_latest_v2.json"
  $packLatest  = Join-Path $Repo "args\data\stage7_as_v1_pack_latest.json"

  $rec = _read_json_retry $reconcileV2 6 80
  if (-not $rec) {
    $out = [ordered]@{
      schema="stage7_as_v1_pack_v2"
      ts_utc=_utc
      status="WARN"
      exit_code=1
      reason="reconcile_v2_missing_or_unreadable"
      path=$reconcileV2
    }
    _write_json $packLatest $out 10
    $out | ConvertTo-Json -Depth 6 -Compress
    exit 1
  }

  $st = _norm_status $rec.status

  # REQUIRED: allow OK or WARN only
  if (($st -ne "OK") -and ($st -ne "WARN")) {
    $out = [ordered]@{
      schema="stage7_as_v1_pack_v2"
      ts_utc=_utc
      status="WARN"
      exit_code=1
      reason="reconcile_v2_not_ok_or_warn"
      reconcile_status=$st
      path=$reconcileV2
    }
    _write_json $packLatest $out 10
    $out | ConvertTo-Json -Depth 6 -Compress
    exit 1
  }

  $py = _pick_python
  $pyExe = [string]$py.exe
  $prefix = @($py.prefix) | ForEach-Object { [string]$_ }

  $logsDir = Join-Path $Repo "args\logs"
  New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

  $logOut = Join-Path $Repo "args\logs\stage7_as_v1_stdout.log"
  $logErr = Join-Path $Repo "args\logs\stage7_as_v1_stderr.log"

  $argsList = @($prefix + @(
    "-m","args.stage7.as_v1",
    "--repo",$Repo,
    "--universe",$Universe,
    "--cooldown-sec",[string]$CooldownSec
  )) | ForEach-Object { [string]$_ }

  $p = Start-Process -FilePath $pyExe -ArgumentList $argsList -NoNewWindow -Wait -PassThru `
        -WorkingDirectory $Repo `
        -RedirectStandardOutput $logOut -RedirectStandardError $logErr

  $rc = [int]$p.ExitCode

  $status = "OK"
  $exitCode = 0
  if ($rc -eq 1) { $status = "WARN"; $exitCode = 1 }
  elseif ($rc -ne 0) { $status = "FAIL"; $exitCode = 2 }

  $out = [ordered]@{
    schema="stage7_as_v1_pack_v2"
    ts_utc=_utc
    status=$status
    exit_code=$exitCode
    repo=$Repo
    reconcile_v2_path=$reconcileV2
    reconcile_v2_status=$st
    python_exe=$pyExe
    python_prefix=@($prefix)
    as_stdout_path=$logOut
    as_stderr_path=$logErr
    as_stderr_tail=(_tail $logErr 80)
  }

  _write_json $packLatest $out 12
  $out | ConvertTo-Json -Depth 8 -Compress
  exit $exitCode

} catch {
  $err = $_.Exception.ToString()
  $repoOut = if ([string]::IsNullOrWhiteSpace($Repo)) { (Resolve-Path (Join-Path $PSScriptRoot "..")).Path } else { $Repo }
  $packLatest  = Join-Path $repoOut "args\data\stage7_as_v1_pack_latest.json"
  $out = [ordered]@{
    schema="stage7_as_v1_pack_v2"
    ts_utc=_utc
    status="FAIL"
    exit_code=2
    error=$err
  }
  _write_json $packLatest $out 12
  $out | ConvertTo-Json -Depth 6 -Compress
  exit 2
}
