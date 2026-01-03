
<# 
STAGE7_AS_V1_PACK (safe)

- Preflight: require reconcile_evidence_latest_v2.json status=OK
- Runs AS v1 (intents + FSM), no trading, no IBKR calls
- Exit codes: 0=OK, 1=WARN (preflight failed), 2=FAIL
#>

[CmdletBinding()]
param(
  [string]$Repo = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }

function _pick_python() {
  $py311 = "C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe"
  if (Test-Path -LiteralPath $py311) { return $py311 }
  return "C:\Windows\py.exe"
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

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
  else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }

  $reconcileV2 = Join-Path $Repo "args\data\reconcile_evidence_latest_v2.json"
  $rec = _read_json_retry $reconcileV2 6 80

  if (-not $rec) {
    Write-Output (@{schema="stage7_as_v1_pack_v1"; ts_utc=_utc; status="WARN"; exit_code=1; reason="reconcile_v2_missing_or_unreadable"; path=$reconcileV2} | ConvertTo-Json -Depth 6 -Compress)
    exit 1
  }

  $st = [string]($rec.status)
  if ($st.ToUpper() -ne "OK") {
    Write-Output (@{schema="stage7_as_v1_pack_v1"; ts_utc=_utc; status="WARN"; exit_code=1; reason="reconcile_v2_not_ok"; reconcile_status=$st; path=$reconcileV2} | ConvertTo-Json -Depth 6 -Compress)
    exit 1
  }

  $pyExe = _pick_python
  $logOut = Join-Path $Repo "args\logs\stage7_as_v1_stdout.log"
  $logErr = Join-Path $Repo "args\logs\stage7_as_v1_stderr.log"

  $argsList = @("-m","args.stage7.as_v1","--repo",$Repo,"--universe","MHG","--cooldown-sec","900")

  $p = Start-Process -FilePath $pyExe -ArgumentList $argsList -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $logOut -RedirectStandardError $logErr

  $rc = [int]$p.ExitCode
  if ($rc -eq 0) { exit 0 }
  if ($rc -eq 1) { exit 1 }
  exit 2

} catch {
  $err = $_.Exception.ToString()
  $fail = @{ schema="stage7_as_v1_pack_v1"; ts_utc=_utc; status="FAIL"; exit_code=2; error=$err }
  $out = Join-Path $Repo "args\data\stage7_as_v1_pack_latest.json"
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $out) | Out-Null
  ($fail | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $out -Encoding UTF8
  exit 2
}
