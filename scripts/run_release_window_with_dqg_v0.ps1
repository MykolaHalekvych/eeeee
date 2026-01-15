param(
  [Parameter(Mandatory=$true)][string]$DqgRequestPath,

  [string]$InnerScript = ".\scripts\product_release_window_v1.ps1",
  [string]$RunId = ("REL_DQG_" + ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"))),
  [string]$OutDir = "",

  [string]$DqgRepo = "C:\Users\mukol\ARGS-DQG-v0",
  [string]$TargetRepo = ".",

  [Parameter(ValueFromRemainingArguments=$true)]
  [object[]]$InnerArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-JsonFile {
  param([string]$Path, [object]$Obj)
  $dir = Split-Path -Parent $Path
  if ($dir -and !(Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
  ($Obj | ConvertTo-Json -Depth 40) | Out-File -FilePath $Path -Encoding utf8
}

try {
  $repo = (Resolve-Path ".").Path

  if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path $repo ("out\release_with_dqg_v0\" + $RunId)
  }
  $outAbs = (Resolve-Path (New-Item -ItemType Directory -Force $OutDir)).Path
  $evidence = Join-Path $outAbs "evidence"
  New-Item -ItemType Directory -Force $evidence | Out-Null

  # 1) Run DQG bridge gate
  $dqgStepOut = Join-Path $outAbs "steps\dqg"
  New-Item -ItemType Directory -Force $dqgStepOut | Out-Null

  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\gate_dqg_bridge_v0.ps1 `
    -DqgRequestPath $DqgRequestPath `
    -RunId ($RunId + "_DQG") `
    -OutDir $dqgStepOut `
    -DqgRepo $DqgRepo `
    -TargetRepo $TargetRepo | Out-Null

  $dqgRc = [int]$LASTEXITCODE
  if ($dqgRc -notin 0,1,2) { $dqgRc = 2 }

  $dqgSum = Join-Path $dqgStepOut "dqg_bridge_summary.json"
  $dqgReason = "DQG.INFRA.IO_ERROR"
  if (Test-Path $dqgSum) {
    $s = Get-Content -Raw $dqgSum | ConvertFrom-Json
    $dqgReason = $s.reason_code
  }

  # 2) Invoke inner ONLY if DQG PASS
  $innerInvoked = $false
  $innerRc = $null

  if ($dqgRc -eq 0) {
    $innerInvoked = $true

    # proof hook: inner can write this file if invoked
    $env:NOOP_HIT_PATH = Join-Path $outAbs "NOOP_WAS_CALLED.txt"

    $innerStdout = Join-Path $evidence "inner.stdout.txt"
    $innerStderr = Join-Path $evidence "inner.stderr.txt"

    $argList = @("-NoProfile","-ExecutionPolicy","Bypass","-File",$InnerScript) + $InnerArgs
    $p = Start-Process -FilePath "powershell" -ArgumentList $argList `
      -WorkingDirectory $repo -NoNewWindow -Wait -PassThru `
      -RedirectStandardOutput $innerStdout -RedirectStandardError $innerStderr

    $innerRc = [int]$p.ExitCode
    if ($innerRc -notin 0,1,2) { $innerRc = 2 }
  }

  # exit_code selection (no (if ...) expressions)
  $exit_code = $dqgRc
  if ($dqgRc -eq 0) {
    if ($innerRc -eq $null) { $exit_code = 0 } else { $exit_code = $innerRc }
  }

  $okFlag = $false
  if ($exit_code -eq 0) { $okFlag = $true }

  $summary = @{
    schema="release_with_dqg_v0"
    ts_utc=(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    ok=$okFlag
    exit_code=$exit_code
    run_id=$RunId
    out_dir=($outAbs -replace "\\","/")
    dqg_exit_code=$dqgRc
    dqg_reason_code=$dqgReason
    inner_invoked=$innerInvoked
    inner_exit_code=$innerRc
    dqg_summary_path=($dqgSum -replace "\\","/")
    inner_script=$InnerScript
  }

  Write-JsonFile -Path (Join-Path $outAbs "summary.json") -Obj $summary
  ($summary | ConvertTo-Json -Compress)
  exit $exit_code
}
catch {
  $fallback = @{
    schema="release_with_dqg_v0"
    ts_utc=(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    ok=$false
    exit_code=2
    reason_code="DQG.INFRA.IO_ERROR"
    error=$_.Exception.Message
  }
  ($fallback | ConvertTo-Json -Compress)
  exit 2
}
