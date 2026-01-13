param(
  [Parameter(Mandatory=$true)][string]$RunId,
  [Parameter(Mandatory=$true)][string]$OutDir,        # absolute or relative to Foundry repo
  [Parameter(Mandatory=$true)][string]$GuardianRepo,  # C:\Users\mukol\ARGS-Guardian-v0
  [Parameter(Mandatory=$true)][string]$RequestPath,
  [Parameter(Mandatory=$true)][string]$PolicyPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_gate_common.ps1"

$ts = ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"))

# Capture Foundry cwd BEFORE changing location
$foundryRoot = (Get-Location).Path

function AbsPath([string]$p, [string]$base) {
  if ([System.IO.Path]::IsPathRooted($p)) { return $p }
  return (Join-Path $base $p)
}

# Make absolute paths so GuardianRepo cwd doesn't break them
$OutDirAbs    = AbsPath $OutDir $foundryRoot
$RequestAbs   = AbsPath $RequestPath $foundryRoot
$PolicyAbs    = AbsPath $PolicyPath $foundryRoot

Ensure-Dir $OutDirAbs

$stdoutPath  = Join-Path $OutDirAbs "guardian_check.stdout.txt"
$stderrPath  = Join-Path $OutDirAbs "guardian_check.stderr.txt"
$summaryPath = Join-Path $OutDirAbs "summary.json"

try {
  Push-Location $GuardianRepo
  try {
    # IMPORTANT: call external process directly; capture stdout/stderr
    $out = & py -3.11 -m args.guardian.guardian_check_v0 `
      --request $RequestAbs `
      --policy $PolicyAbs `
      --out-dir $OutDirAbs 2> $stderrPath

    ($out | Out-String) | Set-Content -Encoding UTF8 $stdoutPath

    $rc = 2
    if ($LASTEXITCODE -ne $null) { $rc = [int]$LASTEXITCODE }

    $parsed = $null
    try {
      $txt = (Get-Content -Raw -Encoding UTF8 $stdoutPath).Trim()
      if ($txt.Length -gt 0) { $parsed = ($txt | ConvertFrom-Json) }
      if ($null -ne $parsed.exit_code) { $rc = [int]$parsed.exit_code }
    } catch {
      # keep rc from $LASTEXITCODE
      $parsed = $null
    }

    $summary = @{
      schema        = "gate_guardian_v0"
      ts_utc        = $ts
      ok            = ($rc -eq 0)
      exit_code     = $rc
      run_id        = $RunId
      guardian_repo = $GuardianRepo
      request_path  = $RequestAbs
      policy_path   = $PolicyAbs
      out_dir       = $OutDirAbs
      stdout_path   = $stdoutPath
      stderr_path   = $stderrPath
    }

    Write-JsonFile $summaryPath $summary
    Write-Json $summary
    exit $rc

  } finally {
    Pop-Location
  }

} catch {
  $summary = @{
    schema    = "gate_guardian_v0"
    ts_utc    = $ts
    ok        = $false
    exit_code = 2
    run_id    = $RunId
    error     = $_.Exception.Message
    out_dir   = $OutDirAbs
  }
  Write-JsonFile $summaryPath $summary
  Write-Json $summary
  exit 2
}
