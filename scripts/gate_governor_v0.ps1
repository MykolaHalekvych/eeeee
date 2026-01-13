param(
  [Parameter(Mandatory=$true)][string]$RunId,
  [Parameter(Mandatory=$true)][string]$OutDir,
  [Parameter(Mandatory=$true)][string]$GovernorRepo,  # C:\Users\mukol\ARGS-Release-Governor-v0
  [Parameter(Mandatory=$true)][string]$BundlePath,
  [Parameter(Mandatory=$true)][string]$PolicyPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_gate_common.ps1"

$ts = ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"))
$foundryRoot = (Get-Location).Path

function AbsPath([string]$p, [string]$base) {
  if ([System.IO.Path]::IsPathRooted($p)) { return $p }
  return (Join-Path $base $p)
}

$OutDirAbs  = AbsPath $OutDir $foundryRoot
$BundleAbs  = AbsPath $BundlePath $foundryRoot
$PolicyAbs  = AbsPath $PolicyPath $foundryRoot

Ensure-Dir $OutDirAbs

$stdoutPath  = Join-Path $OutDirAbs "governor_check.stdout.txt"
$stderrPath  = Join-Path $OutDirAbs "governor_check.stderr.txt"
$summaryPath = Join-Path $OutDirAbs "summary.json"

try {
  Push-Location $GovernorRepo
  try {
    $out = & py -3.11 -m args.governor.governor_check_v0 `
      --bundle $BundleAbs `
      --policy $PolicyAbs `
      --out-dir $OutDirAbs 2> $stderrPath

    ($out | Out-String) | Set-Content -Encoding UTF8 $stdoutPath

    $rc = 2
    if ($LASTEXITCODE -ne $null) { $rc = [int]$LASTEXITCODE }

    # try parse JSON stdout to get exit_code
    try {
      $txt = (Get-Content -Raw -Encoding UTF8 $stdoutPath).Trim()
      if ($txt.Length -gt 0) {
        $parsed = ($txt | ConvertFrom-Json)
        if ($null -ne $parsed.exit_code) { $rc = [int]$parsed.exit_code }
      }
    } catch { }

    $summary = @{
      schema       = "gate_governor_v0"
      ts_utc       = $ts
      ok           = ($rc -eq 0)
      exit_code    = $rc
      run_id       = $RunId
      governor_repo= $GovernorRepo
      bundle_path  = $BundleAbs
      policy_path  = $PolicyAbs
      out_dir      = $OutDirAbs
      stdout_path  = $stdoutPath
      stderr_path  = $stderrPath
    }

    Write-JsonFile $summaryPath $summary
    Write-Json $summary
    exit $rc

  } finally {
    Pop-Location
  }

} catch {
  $summary = @{
    schema    = "gate_governor_v0"
    ts_utc    = $ts
    ok        = $false
    exit_code = 2
    run_id    = $RunId
    error     = $_.Exception.Message
  }
  Write-JsonFile $summaryPath $summary
  Write-Json $summary
  exit 2
}
