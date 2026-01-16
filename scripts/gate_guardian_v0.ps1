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

function Extract-FirstJsonObject([string]$text) {
  if ($null -eq $text) { return $null }
  $start = $text.IndexOf('{')
  if ($start -lt 0) { return $null }

  $depth = 0
  $inString = $false
  $escape = $false

  for ($i = $start; $i -lt $text.Length; $i++) {
    $ch = $text[$i]

    if ($escape) { $escape = $false; continue }

    if ($inString) {
      if ($ch -eq '\') { $escape = $true; continue }
      if ($ch -eq '"') { $inString = $false; continue }
      continue
    }

    if ($ch -eq '"') { $inString = $true; continue }

    if ($ch -eq '{') { $depth++; continue }
    if ($ch -eq '}') {
      $depth--
      if ($depth -eq 0) {
        return $text.Substring($start, $i - $start + 1)
      }
      continue
    }
  }

  return $null
}

function Normalize-Scalar([object]$v) {
  if ($null -eq $v) { return $null }
  if ($v -is [string]) { return $v.Trim() }

  if ($v -is [System.Collections.IEnumerable] -and -not ($v -is [string])) {
    $items = @()
    foreach ($x in $v) {
      if ($null -ne $x) {
        $s = ([string]$x).Trim()
        if ($s.Length -gt 0) { $items += $s }
      }
    }
    if ($items.Count -gt 0) { return ($items -join "|") }
    return $null
  }

  return ([string]$v).Trim()
}

# Make absolute paths so GuardianRepo cwd doesn't break them
$OutDirAbs  = AbsPath $OutDir $foundryRoot
$RequestAbs = AbsPath $RequestPath $foundryRoot
$PolicyAbs  = AbsPath $PolicyPath $foundryRoot

Ensure-Dir $OutDirAbs

$stdoutPath    = Join-Path $OutDirAbs "guardian_check.stdout.txt"
$stderrPath    = Join-Path $OutDirAbs "guardian_check.stderr.txt"
$summaryPath   = Join-Path $OutDirAbs "summary.json"
$childJsonPath = Join-Path $OutDirAbs "guardian_child_extracted.json"

# Best-effort: read request schema for debugging
$request_schema = $null
try {
  if (Test-Path $RequestAbs) {
    $rqRaw = Get-Content -Raw -Encoding UTF8 $RequestAbs
    $rqObj = $rqRaw | ConvertFrom-Json
    $request_schema = Normalize-Scalar $rqObj.schema
  }
} catch { }

try {
  Push-Location $GuardianRepo
  try {
    # Call external process directly; capture stdout/stderr
    $out = & py -3.11 -m args.guardian.guardian_check_v0 `
      --request $RequestAbs `
      --policy $PolicyAbs `
      --out-dir $OutDirAbs 2> $stderrPath

    ($out | Out-String) | Set-Content -Encoding UTF8 $stdoutPath

    $rc = 2
    if ($LASTEXITCODE -ne $null) { $rc = [int]$LASTEXITCODE }

    # Parse child JSON from stdout (robust: extract first JSON object)
    $child = $null
    $child_json = $null
    $parsed_ok = $false

    try {
      $raw = Get-Content -Raw -Encoding UTF8 $stdoutPath
      $child_json = Extract-FirstJsonObject $raw
      if ($child_json) {
        $child_json | Set-Content -Encoding UTF8 $childJsonPath
        $child = $child_json | ConvertFrom-Json
        $parsed_ok = $true
      }
    } catch {
      $child = $null
      $parsed_ok = $false
    }

    # If child provides exit_code, trust it
    if ($null -ne $child -and $null -ne $child.exit_code) {
      try { $rc = [int]$child.exit_code } catch { }
    }

    $base_reason = "INFRA_GUARDIAN"
    if ($rc -eq 0) { $base_reason = "ALLOW_OK" }
    elseif ($rc -eq 1) { $base_reason = "DENY_GUARDIAN" }

    $child_schema = $null
    $child_reason = $null
    $child_out_dir_val = $null
    $child_json_path_val = $null

    if ($null -ne $child) {
      $child_schema = Normalize-Scalar $child.schema
      $child_reason = Normalize-Scalar $child.reason_code
      $child_out_dir_val = Normalize-Scalar $child.out_dir
    }
    if ($child_json) {
      $child_json_path_val = $childJsonPath
    }

    # If no child reason, fall back to base
    if (-not $child_reason -or $child_reason.Length -eq 0) {
      $child_reason = $base_reason
    }

    # Compose wrapper reason_code (avoid duplicates)
    $reason_code = $base_reason
    if ($child_reason -and $child_reason.Length -gt 0) {
      if ($child_reason -eq $base_reason -or $child_reason.StartsWith($base_reason + "|")) {
        $reason_code = $child_reason
      } else {
        $reason_code = $base_reason + "|" + $child_reason
      }
    }

    # Optional expected/got fields, if child exposes them
    $expected_schema = $null
    $got_schema = $null
    if ($null -ne $child) {
      foreach ($n in @("expected_schema","expected_request_schema")) {
        if ($child.PSObject.Properties.Name -contains $n) {
          $expected_schema = Normalize-Scalar $child.$n
        }
      }
      foreach ($n in @("got_schema","got_request_schema")) {
        if ($child.PSObject.Properties.Name -contains $n) {
          $got_schema = Normalize-Scalar $child.$n
        }
      }
    }

    $summary = [ordered]@{
      schema            = "gate_guardian_v0"
      ts_utc            = $ts
      ok                = ($rc -eq 0)
      exit_code         = $rc
      reason_code       = $reason_code
      child_reason_code = $child_reason

      run_id            = $RunId
      guardian_repo     = $GuardianRepo
      request_path      = $RequestAbs
      request_schema    = $request_schema
      policy_path       = $PolicyAbs

      out_dir           = $OutDirAbs
      stdout_path       = $stdoutPath
      stderr_path       = $stderrPath

      parsed_ok         = $parsed_ok
      child_schema      = $child_schema
      child_out_dir     = $child_out_dir_val
      child_json_path   = $child_json_path_val

      expected_schema   = $expected_schema
      got_schema        = $got_schema
    }

    Write-JsonFile $summaryPath $summary
    Write-Json $summary
    exit $rc

  } finally {
    Pop-Location
  }

} catch {
  $summary = [ordered]@{
    schema            = "gate_guardian_v0"
    ts_utc            = $ts
    ok                = $false
    exit_code         = 2
    reason_code       = "INFRA_GUARDIAN|INFRA_EXCEPTION"
    child_reason_code = "INFRA_EXCEPTION"
    run_id            = $RunId
    error             = $_.Exception.Message
    out_dir           = $OutDirAbs
    request_path      = $RequestAbs
    policy_path       = $PolicyAbs
  }
  Write-JsonFile $summaryPath $summary
  Write-Json $summary
  exit 2
}
