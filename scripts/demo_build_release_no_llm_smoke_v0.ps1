
param(
  [Parameter(Mandatory=$false)][string]$KitId = "kit_cli_tool_v1",
  [Parameter(Mandatory=$false)][string]$ProductId = "",
  [Parameter(Mandatory=$false)][string]$Summary = "",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][string]$BuildScript = "scripts\run_factory_app_build_release_no_llm_v1.ps1",
  [Parameter(Mandatory=$false)][string]$Kits = ""   # optional override for kit_registry_v1 --kits
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

function ResolveMaybe([string]$base, [string]$p) {
  if ([string]::IsNullOrWhiteSpace($p)) { return "" }
  if ([System.IO.Path]::IsPathRooted($p)) { return $p }
  return (Join-Path $base $p)
}

try {
  $repo = (Get-Location).Path
  if (-not (Test-Path (Join-Path $repo ".args_engine_repo"))) { throw "WRONG_REPO" }
  if (Test-Path (Join-Path $repo "stop.flag")) { throw "HALT_STOP_FLAG_PRESENT" }

  $t0 = Get-Date

  $run_id  = "BUILD_NO_LLM_SMOKE_{0}_{1}" -f (UtcNowId), (RandHex 4)
  $out_dir = Join-Path $repo ("args\data\smoke\build_release_no_llm_smoke_v0\{0}" -f $run_id)
  New-Item -ItemType Directory -Force -Path $out_dir | Out-Null

  if ([string]::IsNullOrWhiteSpace($Summary)) { $Summary = ("No-LLM BuildRelease smoke {0}" -f $run_id) }

  $build_path = Join-Path $repo $BuildScript
  if (-not (Test-Path $build_path)) { throw ("BUILD_SCRIPT_NOT_FOUND: " + $build_path) }

  # --- Determine product_id ---
  $product_source = "override"
  $kit_stdout_path = Join-Path $out_dir "kit_job_request.stdout.txt"
  $kit_stderr_path = Join-Path $out_dir "kit_job_request.stderr.txt"

  if ([string]::IsNullOrWhiteSpace($ProductId)) {
    $product_source = "kit_default"

    $argv = @("--kit-id",$KitId)
    if (-not [string]::IsNullOrWhiteSpace($Kits)) { $argv += @("--kits",$Kits) }

    $kit_out = & py -3.11 -m args.foundry.kit_registry_v1 @argv 2> $kit_stderr_path
    $kit_rc_raw = $LASTEXITCODE
    $kit_txt = JoinStdout $kit_out
    WriteUtf8NoBom $kit_stdout_path $kit_txt

    if ($kit_rc_raw -ne 0) {
      $out = [ordered]@{
        schema="demo_build_release_no_llm_smoke_v0"
        ts_utc=UtcNowIso
        ok=$false
        exit_code=$RC_FAIL
        repo=$repo
        run_id=$run_id
        out_dir=$out_dir
        kit_id=$KitId
        reason="kit_registry_failed"
        kit_rc_raw=$kit_rc_raw
        kit_stdout_path=$kit_stdout_path
        kit_stderr_path=$kit_stderr_path
      }
      ($out | ConvertTo-Json -Compress -Depth 10)
      exit $RC_FAIL
    }

    $job = $null
    try { $job = ($kit_txt.Trim() | ConvertFrom-Json -ErrorAction Stop) } catch { $job = $null }
    if ($null -eq $job) {
      $out = [ordered]@{
        schema="demo_build_release_no_llm_smoke_v0"
        ts_utc=UtcNowIso
        ok=$false
        exit_code=$RC_INFRA
        repo=$repo
        run_id=$run_id
        out_dir=$out_dir
        kit_id=$KitId
        reason="kit_job_request_not_json"
        kit_stdout_path=$kit_stdout_path
        kit_stderr_path=$kit_stderr_path
      }
      ($out | ConvertTo-Json -Compress -Depth 10)
      exit $RC_INFRA
    }

    $p = ""
    try { $p = [string]$job.product_id } catch { $p = "" }
    if ([string]::IsNullOrWhiteSpace($p)) {
      $out = [ordered]@{
        schema="demo_build_release_no_llm_smoke_v0"
        ts_utc=UtcNowIso
        ok=$false
        exit_code=$RC_FAIL
        repo=$repo
        run_id=$run_id
        out_dir=$out_dir
        kit_id=$KitId
        reason="kit_default_product_id_missing"
        kit_stdout_path=$kit_stdout_path
        kit_stderr_path=$kit_stderr_path
      }
      ($out | ConvertTo-Json -Compress -Depth 10)
      exit $RC_FAIL
    }

    $ProductId = $p
  }

  # --- Run build script ---
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
      product_id_source=$product_source
      run_acceptance=$RunAcceptance
      reason="build_stdout_not_json"
      stdout_path=$stdout_path
      stderr_path=$stderr_path
      rc_raw=$rc_raw
      kit_job_request_stdout=$(if ($product_source -eq "kit_default") { $kit_stdout_path } else { "" })
      kit_job_request_stderr=$(if ($product_source -eq "kit_default") { $kit_stderr_path } else { "" })
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

  $releaseZipAbs = ResolveMaybe $repo ([string]$releaseZip)
  $releaseZipExists = $false
  if (-not [string]::IsNullOrWhiteSpace($releaseZipAbs)) {
    if (Test-Path -LiteralPath $releaseZipAbs -PathType Leaf) { $releaseZipExists = $true }
  }

  # Dist exe check (robust)
  $distDirPrimary = Join-Path $repo ("dist\" + $ProductId)
  $exeCount = 0
  $exeSample = ""
  $distDirUsed = $distDirPrimary

  if (Test-Path -LiteralPath $distDirPrimary -PathType Container) {
    $exes = Get-ChildItem -LiteralPath $distDirPrimary -Recurse -Filter *.exe -ErrorAction SilentlyContinue
    $exeCount = @($exes).Count
    if ($exeCount -gt 0) { $exeSample = [string](@($exes)[0].FullName) }
  }

  if ($exeCount -lt 1) {
    # fallback: find newest exe under dist modified after start time
    $distRoot = Join-Path $repo "dist"
    if (Test-Path -LiteralPath $distRoot -PathType Container) {
      $cand = Get-ChildItem -LiteralPath $distRoot -Recurse -Filter *.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $t0.AddSeconds(-2) } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
      if ($null -ne $cand) {
        $exeCount = 1
        $exeSample = [string]$cand.FullName
        $distDirUsed = [string]$cand.DirectoryName
      }
    }
  }

  $test_ok = $true
  $why = "OK"

  if ($rc_norm -ne 0) { $test_ok = $false; $why = ("EXPECTED_RC_0_GOT_{0}" -f $rc_norm) }
  elseif (($okField -is [bool]) -and (-not $okField)) { $test_ok = $false; $why = "BUILD_JSON_OK_FALSE" }
  elseif (($RunAcceptance -eq "YES") -and ($accOk -is [bool]) -and (-not $accOk)) { $test_ok = $false; $why = "ACCEPTANCE_OK_FALSE" }
  elseif (($RunAcceptance -eq "YES") -and ($accOk -eq $null)) { $test_ok = $false; $why = "ACCEPTANCE_OK_MISSING" }
  elseif (-not $releaseZipExists) { $test_ok = $false; $why = "RELEASE_ZIP_MISSING_OR_NOT_FOUND" }
  elseif ($exeCount -lt 1) { $test_ok = $false; $why = "NO_EXE_IN_DIST" }

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
    product_id_source=$product_source
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
      release_zip_abs=$releaseZipAbs
      release_zip_exists=$releaseZipExists
      dist_dir_primary=$distDirPrimary
      dist_dir_used=$distDirUsed
      exe_count=$exeCount
      exe_sample=$exeSample
      stdout_path=$stdout_path
      stderr_path=$stderr_path
      kit_job_request_stdout=$(if ($product_source -eq "kit_default") { $kit_stdout_path } else { "" })
      kit_job_request_stderr=$(if ($product_source -eq "kit_default") { $kit_stderr_path } else { "" })
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
