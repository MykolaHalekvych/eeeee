param(
  [Parameter(Mandatory=$false)][string]$KitId = "kit_cli_tool_v1",
  [Parameter(Mandatory=$false)][string]$ProductId = "",
  [Parameter(Mandatory=$false)][string]$Summary = "",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][string]$BuildScript = "scripts\run_factory_app_build_release_no_llm_v1.ps1"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) }

function RandHex([int]$n) {
  $bytes = New-Object byte[] $n
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  return ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
}

function WriteUtf8NoBom([string]$p, [string]$s) {
  $enc = New-Object System.Text.UTF8Encoding $false
  [IO.File]::WriteAllText($p, $s, $enc)
}

function JoinStdout([object]$o) {
  if ($null -eq $o) { return "" }
  if ($o -is [System.Array]) { return ($o -join "`n") }
  return [string]$o
}

function NormExit([int]$rc) {
  if ($rc -eq 0 -or $rc -eq 1 -or $rc -eq 2) { return $rc }
  return $RC_INFRA
}

function TryGet([object]$obj, [string]$prop) {
  try {
    if ($null -eq $obj) { return $null }
    if ($obj.PSObject.Properties.Name -contains $prop) { return $obj.$prop }
  } catch {}
  return $null
}

function TryGetNested([object]$obj, [string[]]$path) {
  $cur = $obj
  foreach ($p in $path) {
    $cur = TryGet $cur $p
    if ($null -eq $cur) { return $null }
  }
  return $cur
}

try {
  $repo = (Get-Location).Path
  if (-not (Test-Path (Join-Path $repo ".args_engine_repo"))) { throw "WRONG_REPO" }
  if (Test-Path (Join-Path $repo "stop.flag")) { throw "HALT_STOP_FLAG_PRESENT" }

  $run_id  = "BUILD_NO_LLM_SMOKE_{0}_{1}" -f (UtcNowId), (RandHex 4)
  $out_dir = Join-Path $repo ("args\data\smoke\build_release_no_llm_smoke_v0\{0}" -f $run_id)
  New-Item -ItemType Directory -Force -Path $out_dir | Out-Null

  if ([string]::IsNullOrWhiteSpace($ProductId)) { $ProductId = ("smoke_{0}_{1}" -f $KitId, (RandHex 4)) }
  if ([string]::IsNullOrWhiteSpace($Summary))   { $Summary   = ("No-LLM BuildRelease smoke {0}" -f $run_id) }

  $build_path = Join-Path $repo $BuildScript
  if (-not (Test-Path $build_path)) { throw ("BUILD_SCRIPT_NOT_FOUND: " + $build_path) }

  $stdout_path = Join-Path $out_dir "build.stdout.txt"
  $stderr_path = Join-Path $out_dir "build.stderr.txt"

  $raw = & powershell -NoProfile -ExecutionPolicy Bypass -File $build_path `
    -KitId $KitId `
    -ProductId $ProductId `
    -Summary $Summary `
    -RunAcceptance $RunAcceptance 2> $stderr_path
  $rc_raw = $LASTEXITCODE
  $rc_norm = NormExit $rc_raw

  $txt = JoinStdout $raw
  WriteUtf8NoBom $stdout_path $txt

  $obj = $null
  $parsed_ok = $false
  try {
    $obj = ($txt.Trim() | ConvertFrom-Json -ErrorAction Stop)
    $parsed_ok = $true
  } catch { $parsed_ok = $false }

  if (-not $parsed_ok) {
    $out = [ordered]@{
      schema="demo_build_release_no_llm_smoke_v0"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_INFRA
      repo=$repo
      run_id=$run_id
      out_dir=$out_dir
      kit_id=$KitId
      product_id=$ProductId
      run_acceptance=$RunAcceptance
      reason="build_stdout_not_json"
      stdout_path=$stdout_path
      stderr_path=$stderr_path
      rc_raw=$rc_raw
    }
    ($out | ConvertTo-Json -Compress -Depth 10)
    exit $RC_INFRA
  }

  # Extract common fields (best-effort)
  $okField = TryGet $obj "ok"
  $accOk   = TryGet $obj "acceptance_ok"
  if ($null -eq $accOk) { $accOk = TryGetNested $obj @("build_release","acceptance_ok") }
  if ($null -eq $accOk) { $accOk = TryGetNested $obj @("build_release","build_release","acceptance_ok") }

  $releaseZip = TryGet $obj "release_zip"
  if ($null -eq $releaseZip) { $releaseZip = TryGetNested $obj @("build_release","release_zip") }
  if ($null -eq $releaseZip) { $releaseZip = TryGetNested $obj @("build_release","build_release","release_zip") }

  $releaseId = TryGet $obj "release_id"
  if ($null -eq $releaseId) { $releaseId = TryGetNested $obj @("build_release","release_id") }
  if ($null -eq $releaseId) { $releaseId = TryGetNested $obj @("build_release","build_release","release_id") }

  $runIdOut = TryGet $obj "run_id"
  if ($null -eq $runIdOut) { $runIdOut = TryGetNested $obj @("build_release","run_id") }
  if ($null -eq $runIdOut) { $runIdOut = TryGetNested $obj @("build_release","build_release","run_id") }

  $releaseZipExists = $false
  if (-not [string]::IsNullOrWhiteSpace([string]$releaseZip)) {
    if (Test-Path -LiteralPath ([string]$releaseZip) -PathType Leaf) { $releaseZipExists = $true }
  }

  # Dist exe check (robust): any .exe under dist\<product_id>\
  $distDir = Join-Path $repo ("dist\" + $ProductId)
  $exeCount = 0
  $exeSample = ""
  if (Test-Path -LiteralPath $distDir -PathType Container) {
    $exes = Get-ChildItem -LiteralPath $distDir -Recurse -Filter *.exe -ErrorAction SilentlyContinue
    $exeCount = @($exes).Count
    if ($exeCount -gt 0) { $exeSample = [string](@($exes)[0].FullName) }
  }

  $test_ok = $true
  $why = "OK"

  if ($rc_norm -ne 0) { $test_ok = $false; $why = ("EXPECTED_RC_0_GOT_{0}" -f $rc_norm) }
  elseif (($okField -is [bool]) -and (-not $okField)) { $test_ok = $false; $why = "BUILD_JSON_OK_FALSE" }
  elseif (($RunAcceptance -eq "YES") -and ($accOk -is [bool]) -and (-not $accOk)) { $test_ok = $false; $why = "ACCEPTANCE_OK_FALSE" }
  elseif (-not $releaseZipExists) { $test_ok = $false; $why = "RELEASE_ZIP_MISSING_OR_NOT_FOUND" }
  elseif ($exeCount -lt 1) { $test_ok = $false; $why = "NO_EXE_IN_DIST_PRODUCT_DIR" }

  $exit = $(if ($test_ok) { $RC_OK } elseif ($rc_norm -eq $RC_INFRA) { $RC_INFRA } else { $RC_FAIL })

  $out = [ordered]@{
    schema="demo_build_release_no_llm_smoke_v0"
    ts_utc=UtcNowIso
    ok=($exit -eq 0)
    exit_code=$exit
    repo=$repo
    run_id=$run_id
    out_dir=$out_dir
    kit_id=$KitId
    product_id=$ProductId
    summary=$Summary
    run_acceptance=$RunAcceptance
    expected=[ordered]@{ rc=0; acceptance_ok=$true; release_zip_exists=$true; exe_in_dist=$true }
    observed=[ordered]@{
      rc=$rc_norm
      rc_raw=$rc_raw
      parsed_ok=$parsed_ok
      build_ok=$okField
      acceptance_ok=$accOk
      build_run_id=$runIdOut
      release_id=$releaseId
      release_zip=$releaseZip
      release_zip_exists=$releaseZipExists
      dist_dir=$distDir
      exe_count=$exeCount
      exe_sample=$exeSample
      stdout_path=$stdout_path
      stderr_path=$stderr_path
    }
    reason=$why
  }

  ($out | ConvertTo-Json -Compress -Depth 12)
  exit $exit
}
catch {
  $repo = (Get-Location).Path
  $out = [ordered]@{
    schema="demo_build_release_no_llm_smoke_v0"
    ts_utc=(UtcNowIso)
    ok=$false
    exit_code=$RC_INFRA
    repo=$repo
    error=[ordered]@{ kind="exception"; message=$_.Exception.Message }
  }
  ($out | ConvertTo-Json -Compress -Depth 6)
  exit $RC_INFRA
}
