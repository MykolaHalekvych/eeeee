<# 
STAGE7_AS_V1_PACK (safe)

- Runs AS v1 (intents + FSM) and writes args/data/as_v1_latest.json
- No trading actions, no IBKR calls
- Exit codes: 0=OK, 1=WARN, 2=FAIL
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

try {
  if ([string]::IsNullOrWhiteSpace($Repo)) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path }
  else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }

  $pyExe = _pick_python
  $logOut = Join-Path $Repo "args\logs\stage7_as_v1_stdout.log"
  $logErr = Join-Path $Repo "args\logs\stage7_as_v1_stderr.log"

  $argsList = @("-m","args.stage7.as_v1","--repo",$Repo,"--universe","MHG","--cooldown-sec","900")

  $p = Start-Process -FilePath $pyExe -ArgumentList $argsList -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $logOut -RedirectStandardError $logErr

  $rc = [int]$p.ExitCode

  # normalize to 0/1/2 for ops
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
