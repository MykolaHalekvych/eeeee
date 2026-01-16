param(
  [Parameter(Mandatory=$false)][string]$RunId = "",
  [Parameter(Mandatory=$false)][string]$OutDir = "",
  [Parameter(Mandatory=$false)][int]$Port = 8787
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function UtcNowIso { return ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")) }
function RandHex([int]$n) { -join (1..$n | ForEach-Object { "{0:x}" -f (Get-Random -Max 16) }) }

function CoalesceStr($x, $fallback) {
  if ($null -eq $x) { return $fallback }
  $s = "" + $x
  if ([string]::IsNullOrWhiteSpace($s)) { return $fallback }
  return $s
}

function ToJsonPretty($obj) { return ($obj | ConvertTo-Json -Depth 30) }
function ToJsonLine($obj)   { return ((ToJsonPretty $obj) -replace "(\r?\n)+", "") }

function WriteText([string]$path, $text) {
  $dir = Split-Path -Parent $path
  if ($dir) { New-Item -ItemType Directory -Force $dir | Out-Null }
  if ($null -eq $text) { $text = "" }
  [System.IO.File]::WriteAllText($path, ("" + $text), $utf8NoBom)
}

function WriteJson([string]$path, $obj, [bool]$pretty=$true) {
  $json = if ($pretty) { ToJsonPretty $obj } else { ToJsonLine $obj }
  WriteText $path ($json + "`n")
}

function JoinLines($x) {
  if ($null -eq $x) { return "" }
  if ($x -is [string]) { return $x }
  return (($x | ForEach-Object { "" + $_ }) -join "`n")
}

function FirstNonEmptyLine([string]$s) {
  return (($s -split "(`r`n|`n|`r)") | Where-Object { $_.Trim().Length -gt 0 } | Select-Object -First 1)
}

function StepPaths([string]$outDir, [string]$stepId, [string]$name) {
  $base = ("step_{0}_{1}" -f $stepId, $name)
  return @{
    stdout  = (Join-Path $outDir ($base + ".stdout.txt"))
    stderr  = (Join-Path $outDir ($base + ".stderr.txt"))
    summary = (Join-Path $outDir ($base + ".summary.json"))
  }
}

$repo = (Resolve-Path ".").Path
if (-not $RunId) { $RunId = "WP0_E5_" + (UtcNowId) + "_" + (RandHex 8) }
if (-not $OutDir) { $OutDir = Join-Path $repo ("args\data\smoke\wp0_e5_regression_pack_v1\" + $RunId) }
New-Item -ItemType Directory -Force $OutDir | Out-Null

$steps = @()
function AddStep($s) { $script:steps += $s }

function MakeStep([string]$id, [int]$exitCode, $reason, $child, $paths, $extra) {
  $reason2 = CoalesceStr $reason "INFRA_REASON_NULL_FORBIDDEN"
  $child2  = CoalesceStr $child  "INFRA_CHILD_REASON_NULL_FORBIDDEN"
  $s = @{
    id = $id
    ok = ($exitCode -eq 0)
    exit_code = [int]$exitCode
    reason_code = $reason2
    child_reason_code = $child2
    ts_utc = (UtcNowIso)
    stdout_path = $paths.stdout
    stderr_path = $paths.stderr
    summary_path = $paths.summary
  }
  foreach ($k in $extra.Keys) { $s[$k] = $extra[$k] }
  WriteJson $paths.summary $s $true
  AddStep $s
  return $s
}

try {
  # STEP 01: PASS web_dashboard_v0
  $p = StepPaths $OutDir "01" "pass_web_dashboard_v0"
  $cmd = @("powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",".\scripts\demo_build_release_no_llm_smoke_v0.ps1","-KitId","kit_web_dashboard_v0","-RunAcceptance","YES")
  $out = & $cmd[0] $cmd[1..($cmd.Length-1)] 2> $p.stderr
  $childOut = JoinLines $out
  WriteText $p.stdout $childOut
  $line = FirstNonEmptyLine $childOut

  $obj = $null
  try { if ($line) { $obj = $line | ConvertFrom-Json -ErrorAction Stop } } catch { $obj = $null }

  if ($null -eq $obj) {
    $s = MakeStep "01_pass_web_dashboard_v0" $RC_INFRA "INFRA_BAD_CHILD_JSON" "INFRA_BAD_CHILD_JSON" $p @{ cmd=$cmd; stdout_line=$line }
    throw "STOP_01"
  }

  $childExit = 1
  try { $childExit = [int]$obj.exit_code } catch { $childExit = 1 }
  $okStep = ($childExit -eq 0) -and ($obj.ok -eq $true)

  $exitStep = if ($okStep) { 0 } else { $childExit }
  $reason = if ($okStep) { "OK" } else { "FAIL_SMOKE" }
  $child  = if ($okStep) { "OK" } else { CoalesceStr $obj.reason "FAIL_SMOKE" }

  $s = MakeStep "01_pass_web_dashboard_v0" $exitStep $reason $child $p @{ cmd=$cmd; smoke=$obj }
  if (-not $s.ok) { throw "STOP_01" }

  # STEP 02: PASS cicd_release_pack_v0
  $p = StepPaths $OutDir "02" "pass_cicd_release_pack_v0"
  $cmd = @("powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",".\scripts\demo_build_release_no_llm_smoke_v0.ps1","-KitId","kit_cicd_release_pack_v0","-RunAcceptance","YES")
  $out = & $cmd[0] $cmd[1..($cmd.Length-1)] 2> $p.stderr
  $childOut = JoinLines $out
  WriteText $p.stdout $childOut
  $line = FirstNonEmptyLine $childOut

  $obj = $null
  try { if ($line) { $obj = $line | ConvertFrom-Json -ErrorAction Stop } } catch { $obj = $null }

  if ($null -eq $obj) {
    $s = MakeStep "02_pass_cicd_release_pack_v0" $RC_INFRA "INFRA_BAD_CHILD_JSON" "INFRA_BAD_CHILD_JSON" $p @{ cmd=$cmd; stdout_line=$line }
    throw "STOP_02"
  }

  $childExit = 1
  try { $childExit = [int]$obj.exit_code } catch { $childExit = 1 }
  $okStep = ($childExit -eq 0) -and ($obj.ok -eq $true)

  $exitStep = if ($okStep) { 0 } else { $childExit }
  $reason = if ($okStep) { "OK" } else { "FAIL_SMOKE" }
  $child  = if ($okStep) { "OK" } else { CoalesceStr $obj.reason "FAIL_SMOKE" }

  $s = MakeStep "02_pass_cicd_release_pack_v0" $exitStep $reason $child $p @{ cmd=$cmd; smoke=$obj }
  if (-not $s.ok) { throw "STOP_02" }

  # STEP 03: NEG bind proof
  $p = StepPaths $OutDir "03" "neg_bind_proof"
  $exe = Join-Path $repo "dist\web_dashboard_v0\app.exe"
  if (-not (Test-Path $exe)) {
    WriteText $p.stdout ""
    WriteText $p.stderr ("missing exe: " + $exe)
    $s = MakeStep "03_neg_bind_proof" $RC_INFRA "INFRA_MISSING_EXE" "INFRA_MISSING_EXE" $p @{ exe=$exe }
    throw "STOP_03"
  }

  $tmp = Join-Path $OutDir ("tmp_neg_bind_" + (UtcNowId))
  New-Item -ItemType Directory -Force $tmp | Out-Null
  $flag1 = Join-Path $tmp "stop1.flag"
  $flag2 = Join-Path $tmp "stop2.flag"

  $p1 = Start-Process -FilePath $exe `
    -ArgumentList @("serve","--host","127.0.0.1","--port",$Port,"--stop-flag",$flag1,"--ready-after-ms","0") `
    -PassThru -WindowStyle Hidden

  $listening = $false
  for ($i=0; $i -lt 50; $i++) {
    try {
      $c = New-Object System.Net.Sockets.TcpClient
      $iar = $c.BeginConnect("127.0.0.1",$Port,$null,$null)
      if ($iar.AsyncWaitHandle.WaitOne(200) -and $c.Connected) { $listening = $true; $c.Close(); break }
      $c.Close()
    } catch {}
    Start-Sleep -Milliseconds 200
  }

  if (-not $listening) {
    New-Item -ItemType File -Force $flag1 | Out-Null
    Start-Sleep -Milliseconds 300
    if (-not $p1.HasExited) { Stop-Process -Id $p1.Id -Force }
    WriteText $p.stdout ""
    WriteText $p.stderr "primary not listening"
    $s = MakeStep "03_neg_bind_proof" $RC_INFRA "INFRA_PRIMARY_NOT_LISTENING" "INFRA_PRIMARY_NOT_LISTENING" $p @{ port=$Port; exe=$exe }
    throw "STOP_03"
  }

  $jsonLine = & $exe serve --host 127.0.0.1 --port $Port --stop-flag $flag2 --ready-after-ms 0
  $rcNeg = $LASTEXITCODE

  New-Item -ItemType File -Force $flag1 | Out-Null
  Start-Sleep -Milliseconds 400
  if (-not $p1.HasExited) { Stop-Process -Id $p1.Id -Force }

  $jsonText = JoinLines $jsonLine
  WriteText $p.stdout ("RC=" + $rcNeg + "`nJSON=" + $jsonText)
  WriteText $p.stderr ""

  $okNeg = $false
  $child = "FAIL_NEG_BIND"
  try {
    $o = (FirstNonEmptyLine $jsonText) | ConvertFrom-Json -ErrorAction Stop
    $okNeg = ($rcNeg -eq 2) -and ([int]$o.exit_code -eq 2) -and ($o.reason_code -eq "INFRA_BIND_FAILED") -and ($o.detail -eq "PRECHECK_PORT_IN_USE")
    $child = if ($okNeg) { "OK_NEG_BIND_DETERMINISTIC" } else { "FAIL_NEG_BIND_NOT_DETERMINISTIC" }
  } catch { $okNeg = $false; $child = "FAIL_NEG_BIND_BAD_JSON" }

  $exitStep = if ($okNeg) { 0 } else { 1 }
  $reason = if ($okNeg) { "OK" } else { "FAIL" }
  $s = MakeStep "03_neg_bind_proof" $exitStep $reason $child $p @{ port=$Port; exe=$exe; observed_rc=$rcNeg; observed_json=$jsonText }
  if (-not $s.ok) { throw "STOP_03" }

  # STEP 04: DENY allowlist proof
  $p = StepPaths $OutDir "04" "deny_allowed_paths"
  $denyRun = "E5_DENY_ALLOWED_PATHS_" + (UtcNowId)
  $denyDir = Join-Path $repo ("args\data\runs\" + $denyRun)
  New-Item -ItemType Directory -Force (Join-Path $denyDir "evidence") | Out-Null

  @{
    schema = "job_request_v1"
    kit_id = "kit_web_dashboard_v0"
    product_id = "web_dashboard_v0"
    allowed_paths = @()
  } | ConvertTo-Json -Depth 10 | Out-File -Encoding utf8 (Join-Path $denyDir "job_request.json")

  @{
    schema = "codegen_patch_v0"
    ops = @(
      @{ op = "write_file"; path = "src/app.py"; content = "# deny test`n" }
    )
  } | ConvertTo-Json -Depth 10 | Out-File -Encoding utf8 (Join-Path $denyDir "codegen_output.json")

  $json = & py -3.11 -m args.foundry.workspace_apply_patch_v0 --repo . --product-id web_dashboard_v0 --run-id $denyRun --codegen (Join-Path $denyDir "codegen_output.json") 2> $p.stderr
  $rcDeny = $LASTEXITCODE

  $jsonText = JoinLines $json
  WriteText $p.stdout ("RC=" + $rcDeny + "`nJSON=" + $jsonText)

  $okDeny = $false
  $child = "FAIL_DENY"
  try {
    $o = (FirstNonEmptyLine $jsonText) | ConvertFrom-Json -ErrorAction Stop
    $msg = "" + $o.error.message
    $okDeny = ($rcDeny -eq 1) -and ([int]$o.exit_code -eq 1) -and ($msg -like "*blocked writes outside allowed_paths*")
    $child = if ($okDeny) { "OK_DENY_ALLOWED_PATHS" } else { "FAIL_DENY_NOT_TRIGGERED" }
  } catch { $okDeny = $false; $child = "FAIL_DENY_BAD_JSON" }

  $exitStep = if ($okDeny) { 0 } else { 1 }
  $reason = if ($okDeny) { "OK" } else { "FAIL" }
  $s = MakeStep "04_deny_allowed_paths" $exitStep $reason $child $p @{ deny_run_id=$denyRun; observed_rc=$rcDeny; observed_json=$jsonText }
  if (-not $s.ok) { throw "STOP_04" }

  # FINAL
  $firstBad = ($steps | Where-Object { $_.ok -ne $true } | Select-Object -First 1)
  $allOk = ($null -eq $firstBad)
  $anyInfra = (($steps | Where-Object { $_.exit_code -eq 2 } | Select-Object -First 1) -ne $null)

  $finalExit = if ($allOk) {0} elseif ($anyInfra) {2} else {1}
  $finalChild = if ($allOk) {"OK"} else { CoalesceStr $firstBad.reason_code "FAIL_UNKNOWN" }

  $finalReason = if ($allOk) { "OK" } else { "FAIL" }

  $summary = @{
    schema = "wp0_e5_regression_pack_v1"
    ts_utc = (UtcNowIso)
    ok = $allOk
    exit_code = [int]$finalExit
    reason_code = $finalReason
    child_reason_code = $finalChild
    repo = $repo
    run_id = $RunId
    out_dir = $OutDir
    steps = $steps
  }

  WriteJson (Join-Path $OutDir "summary.json") $summary $true
  Write-Output (ToJsonLine $summary)
  exit $finalExit

} catch {
  $msg = "" + $_
  $final = @{
    schema = "wp0_e5_regression_pack_v1"
    ts_utc = (UtcNowIso)
    ok = $false
    exit_code = 2
    reason_code = "INFRA_EXCEPTION"
    child_reason_code = "INFRA_EXCEPTION"
    repo = $repo
    run_id = $RunId
    out_dir = $OutDir
    error = @{ kind="infra"; message=$msg }
    steps = $steps
  }
  WriteJson (Join-Path $OutDir "summary.json") $final $true
  Write-Output (ToJsonLine $final)
  exit 2
}
