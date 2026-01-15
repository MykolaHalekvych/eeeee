param(
  [string]$RunId = "",
  [string]$OutDir = "",
  [string]$DriftRepo = ""
)

$ErrorActionPreference = "Stop"

function UtcNowIso() { return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ") }

$ToolRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ([string]::IsNullOrWhiteSpace($RunId)) { $RunId = "DRIFT_INTEG_NEG_" + ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")) }
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Join-Path $ToolRoot ("out\drift_integ_neg\" + $RunId) }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

if ([string]::IsNullOrWhiteSpace($DriftRepo)) { $DriftRepo = $env:ARGS_DRIFT_REPO }
if ([string]::IsNullOrWhiteSpace($DriftRepo)) { $DriftRepo = "C:\Users\mukol\ARGS-Drift-Detector-v0" }

$encNoBom = New-Object System.Text.UTF8Encoding($false)
function Write-Utf8NoBom([string]$path, [string]$text) { [System.IO.File]::WriteAllText($path, $text.Replace("`r`n","`n"), $encNoBom) }

function Export-Repo([string]$dst) {
  if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  $prefix = $dst + "\"
  git checkout-index -a -f --prefix="$prefix" | Out-Null
}

function Run-Release([string]$repo, [string]$caseName) {
  $caseOut = Join-Path $OutDir $caseName
  New-Item -ItemType Directory -Force -Path $caseOut | Out-Null
  $stdout = Join-Path $caseOut "stdout.txt"
  $stderr = Join-Path $caseOut "stderr.txt"

  Push-Location $repo
  try {
    # Prefer release window; if missing, fall back to build window.
    $rel = Join-Path $repo "scripts\factory_release_window_v1.ps1"
    $bld = Join-Path $repo "scripts\factory_build_window_v1.ps1"
    if (Test-Path $rel) {
      powershell -NoProfile -ExecutionPolicy Bypass -File $rel 1> $stdout 2> $stderr
    } elseif (Test-Path $bld) {
      powershell -NoProfile -ExecutionPolicy Bypass -File $bld 1> $stdout 2> $stderr
    } else {
      Write-Utf8NoBom $stderr "INFRA: missing factory_release_window_v1.ps1 and factory_build_window_v1.ps1`n"
      return 2
    }
    return $LASTEXITCODE
  }
  finally {
    Pop-Location
  }
}

$tests = @()
$fail = $false

# Case A: ADDED file in scripts/** -> expect exit 1
$repoA = Join-Path $OutDir "repo_added"
Export-Repo $repoA
Write-Utf8NoBom (Join-Path $repoA "scripts\_drift_added_test.txt") "added by integration negative`n"
$rcA = Run-Release $repoA "case_added_file"

# detect gate reason by grepping stdout/stderr
$stdoutA = Get-Content -Raw (Join-Path $OutDir "case_added_file\stdout.txt") -ErrorAction SilentlyContinue
$stderrA = Get-Content -Raw (Join-Path $OutDir "case_added_file\stderr.txt") -ErrorAction SilentlyContinue
$txtA = ($stdoutA + "`n" + $stderrA)

if ($rcA -ne 1) { $fail = $true }
if ($txtA -notmatch "DRIFT\.FAIL\.(ADDED|MODIFIED|REMOVED)_FILE") { $fail = $true }

$tests += [pscustomobject]@{ name="added_file"; expected=1; actual=$rcA }

# Case B: BASELINE missing -> expect exit 2
$repoB = Join-Path $OutDir "repo_baseline_missing"
Export-Repo $repoB

$policyPath = Join-Path $repoB "args\configs\drift_policy_foundry_v0.json"
$baselineId = "foundry_baseline_v0_0002"
if (Test-Path $policyPath) {
  try { $baselineId = [string]((Get-Content -Raw $policyPath | ConvertFrom-Json).baseline_id) } catch {}
}

$baselinePath = Join-Path $repoB ("baselines\drift\" + $baselineId + "\drift_baseline_" + $baselineId + ".json")
if (Test-Path $baselinePath) { Remove-Item -Force $baselinePath }

$rcB = Run-Release $repoB "case_baseline_missing"

$stdoutB = Get-Content -Raw (Join-Path $OutDir "case_baseline_missing\stdout.txt") -ErrorAction SilentlyContinue
$stderrB = Get-Content -Raw (Join-Path $OutDir "case_baseline_missing\stderr.txt") -ErrorAction SilentlyContinue
$txtB = ($stdoutB + "`n" + $stderrB)

if ($rcB -ne 2) { $fail = $true }
if ($txtB -notmatch "DRIFT\.INFRA\.BASELINE_MISSING") { $fail = $true }

$tests += [pscustomobject]@{ name="baseline_missing"; expected=2; actual=$rcB }

$summary = @{
  schema="drift_gate_integration_negative_v0"
  ok=(-not $fail)
  exit_code=($(if ($fail) {1} else {0}))
  ts_utc=(UtcNowIso)
  out_dir=$OutDir
  drift_repo=$DriftRepo
  tests=$tests
}

$json = ($summary | ConvertTo-Json -Depth 10)
Write-Utf8NoBom (Join-Path $OutDir "final_report.json") ($json + "`n")
Write-Output $json
exit ($(if ($fail) {1} else {0}))
