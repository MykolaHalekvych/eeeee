param(
  [Parameter(Mandatory=$true)][string]$RunId,
  [Parameter(Mandatory=$true)][string]$OutDir,
  [Parameter(Mandatory=$true)][string]$GovernorRepo,  # C:\Users\mukol\ARGS-Release-Governor-v0
  [Parameter(Mandatory=$true)][string]$BundlePath,
  [Parameter(Mandatory=$true)][string]$PolicyPath,

  # Optional overrides
  [Parameter(Mandatory=$false)][ValidateSet("auto","ps1","python")][string]$Mode = "auto",
  [Parameter(Mandatory=$false)][string]$ScriptOverride = "",
  [Parameter(Mandatory=$false)][string]$ModuleOverride = "",

  # Deterministic FAIL helper (zip_sha256 mismatch => FAIL=1 for governor_check_v0)
  [Parameter(Mandatory=$false)][ValidateSet("NO","YES")][string]$ChaosBadSha = "NO"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_gate_common.ps1"

$ts = ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"))
$foundryRoot = (Get-Location).Path

function AbsPath([string]$p, [string]$base) {
  if ([string]::IsNullOrWhiteSpace($p)) { return $p }
  if ([System.IO.Path]::IsPathRooted($p)) { return $p }
  return (Join-Path $base $p)
}

function Ensure-File([string]$path, [string]$content = "") {
  try {
    Ensure-Dir (Split-Path -Parent $path)
    if (!(Test-Path $path)) { $content | Set-Content -Encoding UTF8 $path }
  } catch { }
}

function Parse-JsonLast([string]$path) {
  if (!(Test-Path $path)) { return $null }
  $txt = (Get-Content -Raw -Encoding UTF8 $path).Trim()
  if ($txt.Length -eq 0) { return $null }

  try { return ($txt | ConvertFrom-Json) } catch { }

  $lines = @($txt -split "`r?`n" | Where-Object { $_.Trim().Length -gt 0 })
  for ($i = $lines.Length - 1; $i -ge 0; $i--) {
    $l = $lines[$i].Trim()
    if ($l.StartsWith("{") -and $l.EndsWith("}")) {
      try { return ($l | ConvertFrom-Json) } catch { }
    }
  }
  return $null
}

function Get-JsonProp([object]$o, [string]$name) {
  if ($null -eq $o) { return $null }
  $p = $o.PSObject.Properties[$name]
  if ($null -eq $p) { return $null }
  return $p.Value
}

function Find-Ps1Entrypoint([string]$repo, [string]$override) {
  if ($override -and (Test-Path $override)) { return $override }

  # IMPORTANT: generic check is preferred
  $c1 = Join-Path $repo "scripts\governor_check_v0.ps1"
  if (Test-Path $c1) { return $c1 }

  # fallback: release verify (NOT for generic products, but keep as last resort)
  $c2 = Join-Path $repo "scripts\release_verify_governor_v0.ps1"
  if (Test-Path $c2) { return $c2 }

  return $null
}

function Find-PythonModule([string]$repo, [string]$override) {
  if ($override) { return $override }

  $argsDir = Join-Path $repo "args"
  if (!(Test-Path $argsDir)) { return $null }

  $cand = Get-ChildItem -Recurse -File $argsDir -Filter "*check*_v0.py" |
    Where-Object { $_.FullName -match "governor" } |
    Sort-Object FullName |
    Select-Object -First 1

  if (-not $cand) {
    $cand = Get-ChildItem -Recurse -File $argsDir -Filter "*governor*check*.py" |
      Sort-Object FullName |
      Select-Object -First 1
  }

  if (-not $cand) { return $null }

  $rel = $cand.FullName.Substring($repo.Length).TrimStart('\','/')
  return (($rel -replace '\.py$','') -replace '[\\/]', '.')
}

# Absolute paths
$OutDirAbs = AbsPath $OutDir $foundryRoot
$BundleAbs = AbsPath $BundlePath $foundryRoot
$PolicyAbs = AbsPath $PolicyPath $foundryRoot

Ensure-Dir $OutDirAbs

$stdoutPath       = Join-Path $OutDirAbs "governor_check.stdout.txt"
$stderrPath       = Join-Path $OutDirAbs "governor_check.stderr.txt"
$summaryPath      = Join-Path $OutDirAbs "summary.json"
$compatHashesPath = Join-Path $OutDirAbs "bundle_hashes_compat_v0.json"

$baseSummary = @{
  schema           = "gate_governor_v0"
  ts_utc           = $ts
  run_id           = $RunId

  ok               = $false
  exit_code        = 2
  result           = "INFRA"

  governor_repo    = $GovernorRepo
  bundle_path      = $BundleAbs
  policy_path      = $PolicyAbs
  out_dir          = $OutDirAbs

  stdout_path      = $stdoutPath
  stderr_path      = $stderrPath
  compat_hashes_path = $compatHashesPath

  entrypoint_kind  = ""
  entrypoint       = ""

  chaos_bad_sha    = $ChaosBadSha
}

function Emit-And-Exit([hashtable]$s, [int]$rc) {
  Write-JsonFile $summaryPath $s
  Write-Json $s
  exit $rc
}

function Exit-Infra([string]$msg) {
  Ensure-File $stdoutPath ""
  Ensure-File $stderrPath $msg

  $s = $baseSummary.Clone()
  $s.ok = $false
  $s.exit_code = 2
  $s.result = "INFRA"
  $s.error = $msg
  Emit-And-Exit $s 2
}

# Validate inputs
if (!(Test-Path $GovernorRepo)) { Exit-Infra "GovernorRepo not found: $GovernorRepo" }
if (!(Test-Path $BundleAbs))    { Exit-Infra "BundlePath not found: $BundleAbs" }
if (!(Test-Path $PolicyAbs))    { Exit-Infra "PolicyPath not found: $PolicyAbs" }

# Select entrypoint
$ps1 = Find-Ps1Entrypoint $GovernorRepo $ScriptOverride
$mod = Find-PythonModule  $GovernorRepo $ModuleOverride

$use = $null
if ($Mode -eq "ps1") {
  if (-not $ps1) { Exit-Infra "Mode=ps1 but no ps1 entrypoint found in GovernorRepo\scripts" }
  $use = "ps1"
} elseif ($Mode -eq "python") {
  if (-not $mod) { Exit-Infra "Mode=python but no python governor check module found under GovernorRepo\args" }
  $use = "python"
} else {
  if ($ps1) { $use = "ps1" }
  elseif ($mod) { $use = "python" }
  else { Exit-Infra "Cannot find Governor entrypoint (no scripts\governor_check_v0.ps1 / release_verify_governor_v0.ps1 and no python check module)" }
}

# Build hashes JSON compatible with governor_check_v0.ps1 (expects schema release_hashes_v0 + zip_sha256)
$bundleSha = (Get-FileHash -Algorithm SHA256 -Path $BundleAbs).Hash.ToLower()
$zipSha = $bundleSha
if ($ChaosBadSha -eq "YES") { $zipSha = ("0" * 64) }  # deterministic mismatch

$releaseId = [System.IO.Path]::GetFileNameWithoutExtension($BundleAbs)

$hashesObj = @{
  schema       = "release_hashes_v0"
  ts_utc       = $ts
  generated_by = "gate_governor_v0"
  release_id   = $releaseId
  zip_path     = $BundleAbs
  zip_sha256   = $zipSha
  Count        = 1
  files        = @()
}
(Write-Json $hashesObj) | Set-Content -Encoding UTF8 $compatHashesPath

$baseSummary.bundle_sha256 = $bundleSha
$baseSummary.zip_sha256_used = $zipSha
$baseSummary.release_id = $releaseId

try {
  Ensure-File $stdoutPath ""
  Ensure-File $stderrPath ""

  $rc = 2

  if ($use -eq "ps1") {
    $baseSummary.entrypoint_kind = "ps1"
    $baseSummary.entrypoint = $ps1

    $cmdInfo = Get-Command $ps1
    $paramKeys = $cmdInfo.Parameters.Keys

    $invoke = @{}

    function MapArg([string[]]$names, [object]$val) {
      foreach ($n in $names) {
        if ($paramKeys -contains $n) { $invoke[$n] = $val; return $true }
      }
      return $false
    }

    # governor_check_v0 expects Zip + Hashes; Policy/OutDir are optional
    MapArg @("Zip","ZipPath","Bundle","BundlePath","ReleaseZip","ReleasePath","BundleZip") $BundleAbs | Out-Null
    MapArg @("Hashes","HashesPath","HashPath","HashesJsonPath","HashesFile","HashesFilePath") $compatHashesPath | Out-Null
    MapArg @("Policy","PolicyPath") $PolicyAbs | Out-Null
    MapArg @("OutDir","Out","OutRoot","OutPath") $OutDirAbs | Out-Null

    $argList = @("-NoProfile","-ExecutionPolicy","Bypass","-File",$ps1)
    foreach ($kv in ($invoke.GetEnumerator() | Sort-Object Name)) {
      $argList += ("-" + $kv.Key)
      $argList += [string]$kv.Value
    }

    $p = Start-Process `
      -FilePath "powershell.exe" `
      -ArgumentList $argList `
      -WorkingDirectory $GovernorRepo `
      -NoNewWindow `
      -Wait `
      -PassThru `
      -RedirectStandardOutput $stdoutPath `
      -RedirectStandardError  $stderrPath

    $rc = [int]$p.ExitCode

  } else {
    $baseSummary.entrypoint_kind = "python"
    $baseSummary.entrypoint = $mod

    $argList = @("-3.11","-m",$mod,"--bundle",$BundleAbs,"--policy",$PolicyAbs,"--out-dir",$OutDirAbs)

    $p = Start-Process `
      -FilePath "py" `
      -ArgumentList $argList `
      -WorkingDirectory $GovernorRepo `
      -NoNewWindow `
      -Wait `
      -PassThru `
      -RedirectStandardOutput $stdoutPath `
      -RedirectStandardError  $stderrPath

    $rc = [int]$p.ExitCode
  }

  # Parse stdout JSON if present; strict-safe
  $parsed = Parse-JsonLast $stdoutPath

  $ec = Get-JsonProp $parsed "exit_code"
  if ($null -ne $ec) { $rc = [int]$ec }

  $s = $baseSummary.Clone()
  $s.exit_code = $rc
  $s.ok = ($rc -eq 0)
  $s.result = $(if ($rc -eq 0) { "PASS" } elseif ($rc -eq 1) { "FAIL" } else { "INFRA" })

  $dec = Get-JsonProp $parsed "decision"
  if ($null -ne $dec) { $s.decision = $dec }

  $reason = Get-JsonProp $parsed "reason_code"
  if ($null -ne $reason) { $s.reason_code = $reason }

  $rpt = Get-JsonProp $parsed "report_path"
  if ($null -ne $rpt) { $s.governor_report_path = $rpt }

  # attach stderr tail only for INFRA
  if ($rc -eq 2 -and (Test-Path $stderrPath)) {
    $tail = (Get-Content -Encoding UTF8 $stderrPath -Tail 1)
    if ($tail) { $s.error = $tail }
  }

  Emit-And-Exit $s $rc

} catch {
  $msg = $_.Exception.Message
  Ensure-File $stdoutPath ""
  $msg | Set-Content -Encoding UTF8 $stderrPath

  $s = $baseSummary.Clone()
  $s.ok = $false
  $s.exit_code = 2
  $s.result = "INFRA"
  $s.error = $msg
  Emit-And-Exit $s 2
}
