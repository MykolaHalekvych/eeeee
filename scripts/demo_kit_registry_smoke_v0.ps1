param(
  [Parameter(Mandatory=$false)][string]$Kits = ""
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

function PickKitId($listObj) {
  # returns kit_id string or ""
  $ids = @()

  if ($null -eq $listObj) { return "" }

  if ($listObj -is [System.Array]) {
    foreach ($x in $listObj) {
      if ($x -is [string]) { $ids += $x; continue }
      try {
        if ($x.kit_id) { $ids += [string]$x.kit_id; continue }
        if ($x.id) { $ids += [string]$x.id; continue }
      } catch {}
    }
  } else {
    # object — try common shapes: {kits:[...]} or {items:[...]} or {kit_ids:[...]}
    try {
      if ($listObj.kit_ids) {
        foreach ($x in $listObj.kit_ids) { if ($x) { $ids += [string]$x } }
      }
      elseif ($listObj.kits) {
        foreach ($x in $listObj.kits) {
          if ($x -is [string]) { $ids += $x; continue }
          if ($x.kit_id) { $ids += [string]$x.kit_id; continue }
          if ($x.id) { $ids += [string]$x.id; continue }
        }
      }
      elseif ($listObj.items) {
        foreach ($x in $listObj.items) {
          if ($x -is [string]) { $ids += $x; continue }
          if ($x.kit_id) { $ids += [string]$x.kit_id; continue }
          if ($x.id) { $ids += [string]$x.id; continue }
        }
      }
    } catch {}
  }

  if ($ids.Count -eq 0) { return "" }

  # prefer known kit if present
  if ($ids -contains "kit_cli_tool_v0") { return "kit_cli_tool_v0" }
  return [string]$ids[0]
}

try {
  $repo = (Get-Location).Path
  if (-not (Test-Path (Join-Path $repo ".args_engine_repo"))) { throw "WRONG_REPO" }

  $run_id  = "KIT_SMOKE_{0}_{1}" -f (UtcNowId), (RandHex 4)
  $out_dir = Join-Path $repo ("args\data\smoke\kit_registry_smoke_v0\{0}" -f $run_id)
  New-Item -ItemType Directory -Force -Path $out_dir | Out-Null

  $list_stdout_path = Join-Path $out_dir "kit_list.stdout.txt"
  $list_stderr_path = Join-Path $out_dir "kit_list.stderr.txt"
  $gen_stdout_path  = Join-Path $out_dir "job_request.stdout.txt"
  $gen_stderr_path  = Join-Path $out_dir "job_request.stderr.txt"

  # Build argv for list
  $argv_list = @("--list","--json")
  if (-not [string]::IsNullOrWhiteSpace($Kits)) {
    $argv_list += @("--kits",$Kits)
  }

  $list_out = & py -3.11 -m args.foundry.kit_registry_v1 @argv_list 2> $list_stderr_path
  $list_rc_raw = $LASTEXITCODE
  $list_txt = JoinStdout $list_out
  WriteUtf8NoBom $list_stdout_path $list_txt

  if ($list_rc_raw -ne 0) {
    $out = [ordered]@{
      schema="demo_kit_registry_smoke_v0"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_FAIL
      repo=$repo
      run_id=$run_id
      out_dir=$out_dir
      step="list"
      rc_raw=$list_rc_raw
      stdout_path=$list_stdout_path
      stderr_path=$list_stderr_path
      reason="kit_list_failed"
    }
    ($out | ConvertTo-Json -Compress -Depth 10)
    exit $RC_FAIL
  }

  $list_obj = $null
  try { $list_obj = ($list_txt.Trim() | ConvertFrom-Json -ErrorAction Stop) } catch { $list_obj = $null }

  if ($null -eq $list_obj) {
    $out = [ordered]@{
      schema="demo_kit_registry_smoke_v0"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_INFRA
      repo=$repo
      run_id=$run_id
      out_dir=$out_dir
      step="list"
      reason="kit_list_not_json"
      stdout_path=$list_stdout_path
      stderr_path=$list_stderr_path
    }
    ($out | ConvertTo-Json -Compress -Depth 10)
    exit $RC_INFRA
  }

  $kit_id = PickKitId $list_obj
  if ([string]::IsNullOrWhiteSpace($kit_id)) {
    $out = [ordered]@{
      schema="demo_kit_registry_smoke_v0"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_FAIL
      repo=$repo
      run_id=$run_id
      out_dir=$out_dir
      step="select_kit"
      reason="no_kits_found_in_list"
      stdout_path=$list_stdout_path
      stderr_path=$list_stderr_path
    }
    ($out | ConvertTo-Json -Compress -Depth 10)
    exit $RC_FAIL
  }

  # Generate job_request with overrides (this is the “show overrides” proof)
  $prod = "smoke_product_{0}" -f (RandHex 4)
  $sum  = "Kit registry smoke {0}" -f $run_id

  $argv_gen = @("--kit-id",$kit_id,"--product-id",$prod,"--summary",$sum)
  if (-not [string]::IsNullOrWhiteSpace($Kits)) {
    $argv_gen += @("--kits",$Kits)
  }

  $gen_out = & py -3.11 -m args.foundry.kit_registry_v1 @argv_gen 2> $gen_stderr_path
  $gen_rc_raw = $LASTEXITCODE
  $gen_txt = JoinStdout $gen_out
  WriteUtf8NoBom $gen_stdout_path $gen_txt

  if ($gen_rc_raw -ne 0) {
    $out = [ordered]@{
      schema="demo_kit_registry_smoke_v0"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_FAIL
      repo=$repo
      run_id=$run_id
      out_dir=$out_dir
      step="generate"
      rc_raw=$gen_rc_raw
      kit_id=$kit_id
      product_id=$prod
      summary=$sum
      stdout_path=$gen_stdout_path
      stderr_path=$gen_stderr_path
      reason="job_request_failed"
    }
    ($out | ConvertTo-Json -Compress -Depth 10)
    exit $RC_FAIL
  }

  $job = $null
  try { $job = ($gen_txt.Trim() | ConvertFrom-Json -ErrorAction Stop) } catch { $job = $null }

  if ($null -eq $job) {
    $out = [ordered]@{
      schema="demo_kit_registry_smoke_v0"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_INFRA
      repo=$repo
      run_id=$run_id
      out_dir=$out_dir
      step="generate"
      kit_id=$kit_id
      reason="job_request_not_json"
      stdout_path=$gen_stdout_path
      stderr_path=$gen_stderr_path
    }
    ($out | ConvertTo-Json -Compress -Depth 10)
    exit $RC_INFRA
  }

  # Minimal asserts (don’t overfit to internal schema)
  $schema = ""
  $job_kit = ""
  $job_prod = ""

  try { if ($job.schema) { $schema = [string]$job.schema } } catch {}
  try { if ($job.kit_id) { $job_kit = [string]$job.kit_id } } catch {}
  try { if ($job.product_id) { $job_prod = [string]$job.product_id } } catch {}

  $assert_ok = $true
  $why = "OK"

  if ([string]::IsNullOrWhiteSpace($schema)) { $assert_ok = $false; $why = "missing_schema_in_job_request" }
  elseif ($job_kit -ne $kit_id) { $assert_ok = $false; $why = "kit_id_mismatch_expected_{0}_got_{1}" -f $kit_id, $job_kit }
  elseif ($job_prod -ne $prod) { $assert_ok = $false; $why = "product_id_mismatch_expected_{0}_got_{1}" -f $prod, $job_prod }

  $exit = $(if ($assert_ok) { $RC_OK } else { $RC_FAIL })

  $out = [ordered]@{
    schema="demo_kit_registry_smoke_v0"
    ts_utc=UtcNowIso
    ok=($exit -eq 0)
    exit_code=$exit
    repo=$repo
    run_id=$run_id
    out_dir=$out_dir
    list = [ordered]@{
      rc_raw=$list_rc_raw
      stdout_path=$list_stdout_path
      stderr_path=$list_stderr_path
    }
    generate = [ordered]@{
      rc_raw=$gen_rc_raw
      kit_id=$kit_id
      product_id=$prod
      summary=$sum
      stdout_path=$gen_stdout_path
      stderr_path=$gen_stderr_path
      observed_schema=$schema
      observed_kit_id=$job_kit
      observed_product_id=$job_prod
    }
    reason=$why
  }

  ($out | ConvertTo-Json -Compress -Depth 10)
  exit $exit
}
catch {
  $repo = (Get-Location).Path
  $out = [ordered]@{
    schema="demo_kit_registry_smoke_v0"
    ts_utc=(UtcNowIso)
    ok=$false
    exit_code=$RC_INFRA
    repo=$repo
    error=[ordered]@{ kind="exception"; message=$_.Exception.Message }
  }
  ($out | ConvertTo-Json -Compress -Depth 6)
  exit $RC_INFRA
}