param(
  [Parameter(Mandatory=$false)][string]$Kits = "",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][string]$BuildSmokeScript = "scripts\demo_build_release_no_llm_smoke_v0.ps1"
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

try {
  $repo = (Get-Location).Path
  if (-not (Test-Path (Join-Path $repo ".args_engine_repo"))) { throw "WRONG_REPO" }
  if (Test-Path (Join-Path $repo "stop.flag")) { throw "HALT_STOP_FLAG_PRESENT" }

  $run_id  = "TEMPLATE_PACK_MATRIX_{0}_{1}" -f (UtcNowId), (RandHex 4)
  $out_dir = Join-Path $repo ("args\data\smoke\template_pack_matrix_v0\{0}" -f $run_id)
  New-Item -ItemType Directory -Force -Path $out_dir | Out-Null

  $list_stdout = Join-Path $out_dir "kit_list.stdout.json"
  $list_stderr = Join-Path $out_dir "kit_list.stderr.txt"

  $argv = @("--list","--json")
  if (-not [string]::IsNullOrWhiteSpace($Kits)) { $argv += @("--kits",$Kits) }

  $list_out = & py -3.11 -m args.foundry.kit_registry_v1 @argv 2> $list_stderr
  $list_rc_raw = $LASTEXITCODE
  $list_txt = JoinStdout $list_out
  WriteUtf8NoBom $list_stdout $list_txt

  if ($list_rc_raw -ne 0) {
    $out = [ordered]@{
      schema="demo_template_pack_matrix_v0"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_FAIL
      repo=$repo
      run_id=$run_id
      out_dir=$out_dir
      reason="kit_list_failed"
      kit_list_rc_raw=$list_rc_raw
      kit_list_stdout=$list_stdout
      kit_list_stderr=$list_stderr
    }
    ($out | ConvertTo-Json -Compress -Depth 10)
    exit $RC_FAIL
  }

  $kits_obj = $null
  try { $kits_obj = ($list_txt.Trim() | ConvertFrom-Json -ErrorAction Stop) } catch { $kits_obj = $null }
  if ($null -eq $kits_obj -or -not ($kits_obj -is [System.Array])) {
    $out = [ordered]@{
      schema="demo_template_pack_matrix_v0"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_INFRA
      repo=$repo
      run_id=$run_id
      out_dir=$out_dir
      reason="kit_list_not_json_array"
      kit_list_stdout=$list_stdout
      kit_list_stderr=$list_stderr
    }
    ($out | ConvertTo-Json -Compress -Depth 10)
    exit $RC_INFRA
  }

  $build_smoke = Join-Path $repo $BuildSmokeScript
  if (-not (Test-Path $build_smoke)) { throw ("BUILD_SMOKE_SCRIPT_NOT_FOUND: " + $build_smoke) }

  $cases = @()
  $any_infra = $false
  $any_fail = $false

  foreach ($k in $kits_obj) {
    $kit_id = ""
    $default_product = ""
    try { $kit_id = [string]$k.kit_id } catch { $kit_id = "" }
    try { $default_product = [string]$k.default_product_id } catch { $default_product = "" }

    if ([string]::IsNullOrWhiteSpace($kit_id)) { continue }

    $c_stdout = Join-Path $out_dir ("build_{0}.stdout.json" -f $kit_id)
    $c_stderr = Join-Path $out_dir ("build_{0}.stderr.txt" -f $kit_id)

    $raw = & powershell -NoProfile -ExecutionPolicy Bypass -File $build_smoke -KitId $kit_id -RunAcceptance $RunAcceptance 2> $c_stderr
    $rc_raw = $LASTEXITCODE
    $rc = NormExit $rc_raw

    $txt = JoinStdout $raw
    WriteUtf8NoBom $c_stdout $txt

    $obj = $null
    $parsed_ok = $false
    try { $obj = ($txt.Trim() | ConvertFrom-Json -ErrorAction Stop); $parsed_ok = $true } catch { $parsed_ok = $false }

    if (-not $parsed_ok) { $rc = $RC_INFRA }
    if ($rc -eq 2) { $any_infra = $true }
    elseif ($rc -ne 0) { $any_fail = $true }

    $cases += [ordered]@{
      kit_id=$kit_id
      default_product_id=$default_product
      rc=$rc
      rc_raw=$rc_raw
      parsed_ok=$parsed_ok
      ok=$(if ($parsed_ok -and ($obj.PSObject.Properties.Name -contains "ok")) { [bool]$obj.ok } else { $false })
      product_id=$(if ($parsed_ok) { [string]($obj.product_id) } else { "" })
      release_id=$(if ($parsed_ok -and ($obj.PSObject.Properties.Name -contains "observed")) { [string]($obj.observed.release_id) } else { "" })
      release_zip=$(if ($parsed_ok -and ($obj.PSObject.Properties.Name -contains "observed")) { [string]($obj.observed.release_zip) } else { "" })
      stdout_path=$c_stdout
      stderr_path=$c_stderr
    }
  }

  $exit = $RC_OK
  if ($any_infra) { $exit = $RC_INFRA }
  elseif ($any_fail) { $exit = $RC_FAIL }

  $out = [ordered]@{
    schema="demo_template_pack_matrix_v0"
    ts_utc=UtcNowIso
    ok=($exit -eq 0)
    exit_code=$exit
    repo=$repo
    run_id=$run_id
    out_dir=$out_dir
    run_acceptance=$RunAcceptance
    kit_list_stdout=$list_stdout
    kit_list_stderr=$list_stderr
    cases=$cases
    reason="OK"
  }

  ($out | ConvertTo-Json -Compress -Depth 12)
  exit $exit
}
catch {
  $repo = (Get-Location).Path
  $out = [ordered]@{
    schema="demo_template_pack_matrix_v0"
    ts_utc=(UtcNowIso)
    ok=$false
    exit_code=$RC_INFRA
    repo=$repo
    error=[ordered]@{ kind="exception"; message=$_.Exception.Message }
  }
  ($out | ConvertTo-Json -Compress -Depth 6)
  exit $RC_INFRA
}
