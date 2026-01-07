param(
  [Parameter(Mandatory=$true)][string]$RunId,
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exit codes (project standard)
$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function New-UtcIso { return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ") }

function Ensure-Dir([string]$p) {
  if (-not (Test-Path -Path $p -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $p | Out-Null
  }
}

function Safe-ReadTextUtf8([string]$Path) {
  $raw = ""
  try { $raw = Get-Content -Raw -Encoding UTF8 -Path $Path -ErrorAction Stop } catch { $raw = "" }
  if ($null -eq $raw) { $raw = "" }
  return ($raw -replace "^\uFEFF","")
}

function Write-RunEvent {
  param(
    [string]$RunDir,[string]$RunId,[string]$Step,[string]$Status,[bool]$Ok,[int]$ExitCode,[hashtable]$Details
  )
  Ensure-Dir $RunDir
  $eventsPath = Join-Path $RunDir "events.jsonl"
  $ev = [ordered]@{
    schema    = "factory_run_event_v1"
    ts_utc    = (New-UtcIso)
    run_id    = $RunId
    step      = $Step
    status    = $Status
    ok        = $Ok
    exit_code = $ExitCode
    details   = $Details
  }
  Add-Content -Encoding utf8 -Path $eventsPath -Value (($ev | ConvertTo-Json -Compress -Depth 30))
}

function Write-FinalReportFile {
  param([string]$RunDir,[hashtable]$Report)
  Ensure-Dir $RunDir
  $path = Join-Path $RunDir "final_report.json"
  $tmp  = "$path.tmp"
  ($Report | ConvertTo-Json -Depth 30) | Set-Content -Encoding utf8 -Path $tmp
  Move-Item -Force -Path $tmp -Destination $path
}

function Invoke-Step {
  param([string]$RunDir,[string]$RunId,[string]$StepName,[string]$CmdLine,[string]$EvidenceDir)

  Ensure-Dir $EvidenceDir
  $stdoutPath = Join-Path $EvidenceDir ("{0}.stdout.txt" -f $StepName)
  $stderrPath = Join-Path $EvidenceDir ("{0}.stderr.txt" -f $StepName)
  if (Test-Path $stdoutPath) { Remove-Item -Force $stdoutPath }
  if (Test-Path $stderrPath) { Remove-Item -Force $stderrPath }

  Write-RunEvent -RunDir $RunDir -RunId $RunId -Step $StepName -Status "START" -Ok $true -ExitCode 0 -Details @{
    cmd=$CmdLine; stdout_path=$stdoutPath; stderr_path=$stderrPath
  }

  $p = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $CmdLine) -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

  $rc = [int]$p.ExitCode
  $stdout = Safe-ReadTextUtf8 $stdoutPath

  return [ordered]@{ rc=$rc; stdout=$stdout; stdout_path=$stdoutPath; stderr_path=$stderrPath }
}

function Fail-And-Exit {
  param(
    [string]$RunDir,[string]$RunId,[string]$ProductId,[string]$Step,[int]$ExitCode,[string]$EvidenceDir,
    [string]$LastOutputPath,[string]$LastStdout,[string]$LastStdoutPath,[string]$LastStderrPath
  )

  Write-RunEvent -RunDir $RunDir -RunId $RunId -Step $Step -Status "FAIL" -Ok $false -ExitCode $ExitCode -Details @{
    evidence_dir=$EvidenceDir; last_output=$LastOutputPath; last_stdout_path=$LastStdoutPath; last_stderr_path=$LastStderrPath
  }

  $final = [ordered]@{
    schema            = "factory_final_report_v1"
    ok                = $false
    exit_code         = $ExitCode
    run_id            = $RunId
    product_id        = $ProductId
    run_dir           = $RunDir
    failed_step       = $Step
    evidence_dir      = $EvidenceDir
    events_jsonl      = (Join-Path $RunDir "events.jsonl")
    final_report_json = (Join-Path $RunDir "final_report.json")
    last_output_path  = $LastOutputPath
    last_stdout_path  = $LastStdoutPath
    last_stderr_path  = $LastStderrPath
    last_stdout       = $LastStdout
  }

  Write-FinalReportFile -RunDir $RunDir -Report $final
  ($final | ConvertTo-Json -Compress -Depth 30)
  exit $ExitCode
}

function Preflight-Missing-File {
  param(
    [string]$RunDir,[string]$RunId,[string]$EvidenceDir,[string]$StepName,[string]$MissingPath,[string]$Message
  )

  Ensure-Dir $EvidenceDir

  $pre = [ordered]@{
    schema    = "factory_preflight_missing_file_v1"
    ts_utc    = (New-UtcIso)
    ok        = $false
    exit_code = $RC_FAIL
    run_id    = $RunId
    run_dir   = $RunDir
    missing   = [ordered]@{ path=$MissingPath; message=$Message }
  }
  $preJson = ($pre | ConvertTo-Json -Compress -Depth 30)

  $outPath = Join-Path $RunDir ("{0}.json" -f $StepName)
  $errPath = Join-Path $EvidenceDir ("{0}.stderr.txt" -f $StepName)

  $preJson | Set-Content -Encoding utf8 -Path $outPath
  $Message | Set-Content -Encoding utf8 -Path $errPath

  Fail-And-Exit -RunDir $RunDir -RunId $RunId -ProductId "" -Step $StepName -ExitCode $RC_FAIL -EvidenceDir $EvidenceDir `
    -LastOutputPath $outPath -LastStdout $preJson -LastStdoutPath "" -LastStderrPath $errPath
}

# ---------------- MAIN ----------------

$repoResolved = $Repo
$run_dir = ""
$EvidenceDir = ""
$product_id = ""

try {
  if (-not $RunId -or $RunId.Trim().Length -lt 5) {
    ($([ordered]@{schema="factory_final_report_v1"; ok=$false; exit_code=$RC_FAIL; run_id=$RunId; failed_step="preflight"; error="RunId is required"}) | ConvertTo-Json -Compress -Depth 10)
    exit $RC_FAIL
  }

  try { $repoResolved = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { $repoResolved = $Repo }

  if (-not (Test-Path -Path $repoResolved -PathType Container)) {
    ($([ordered]@{schema="factory_final_report_v1"; ok=$false; exit_code=$RC_INFRA; run_id=$RunId; failed_step="preflight"; error=("Repo path not found: {0}" -f $repoResolved)}) | ConvertTo-Json -Compress -Depth 10)
    exit $RC_INFRA
  }

  Push-Location -Path $repoResolved

  $run_dir = Join-Path (Join-Path $repoResolved 'args\data\runs') $RunId
  if (-not (Test-Path -Path $run_dir -PathType Container)) {
    ($([ordered]@{schema="factory_final_report_v1"; ok=$false; exit_code=$RC_FAIL; run_id=$RunId; failed_step="preflight"; error=("run_dir not found: {0}" -f $run_dir)}) | ConvertTo-Json -Compress -Depth 10)
    exit $RC_FAIL
  }

  $EvidenceDir = Join-Path $run_dir "evidence"
  Ensure-Dir $EvidenceDir

  Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "build_release" -Status "START" -Ok $true -ExitCode 0 -Details @{
    repo=$repoResolved; evidence_dir=$EvidenceDir; run_acceptance=$RunAcceptance
  }

  $jr_path = Join-Path $run_dir 'job_request.json'
  $codegen_path = Join-Path $run_dir 'codegen_output.json'

  if (-not (Test-Path -Path $jr_path -PathType Leaf)) {
    Preflight-Missing-File -RunDir $run_dir -RunId $RunId -EvidenceDir $EvidenceDir -StepName "preflight_missing_job_request" `
      -MissingPath $jr_path -Message "job_request.json missing in run_dir"
  }

  if (-not (Test-Path -Path $codegen_path -PathType Leaf)) {
    Preflight-Missing-File -RunDir $run_dir -RunId $RunId -EvidenceDir $EvidenceDir -StepName "preflight_missing_codegen_output" `
      -MissingPath $codegen_path -Message "codegen_output.json missing in run_dir"
  }

  # Load job_request (FAIL=1 on empty/parse issues)
  $jr_raw = Safe-ReadTextUtf8 $jr_path
  if ([string]::IsNullOrWhiteSpace($jr_raw)) {
    $msg = "job_request.json empty"
    $errPath = Join-Path $EvidenceDir "load_job_request.stderr.txt"
    $msg | Set-Content -Encoding utf8 -Path $errPath
    Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId "" -Step "load_job_request" -ExitCode $RC_FAIL -EvidenceDir $EvidenceDir `
      -LastOutputPath $jr_path -LastStdout "" -LastStdoutPath "" -LastStderrPath $errPath
  }

  try { $jr = ($jr_raw | ConvertFrom-Json) } catch {
    $msg = "job_request.json parse failed: " + $_.Exception.Message
    $errPath = Join-Path $EvidenceDir "load_job_request.stderr.txt"
    $msg | Set-Content -Encoding utf8 -Path $errPath
    Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId "" -Step "load_job_request" -ExitCode $RC_FAIL -EvidenceDir $EvidenceDir `
      -LastOutputPath $jr_path -LastStdout $jr_raw -LastStdoutPath "" -LastStderrPath $errPath
  }

  $product_id = [string]$jr.product_id
  if ([string]::IsNullOrWhiteSpace($product_id)) {
    $msg = "job_request.json missing product_id"
    $errPath = Join-Path $EvidenceDir "load_job_request.stderr.txt"
    $msg | Set-Content -Encoding utf8 -Path $errPath
    Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId "" -Step "load_job_request" -ExitCode $RC_FAIL -EvidenceDir $EvidenceDir `
      -LastOutputPath $jr_path -LastStdout $jr_raw -LastStdoutPath "" -LastStderrPath $errPath
  }

  Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "load_job_request" -Status "OK" -Ok $true -ExitCode 0 -Details @{
    product_id=$product_id; job_request=$jr_path; codegen_output=$codegen_path
  }

  # 1) Apply patch
  $cmd = "py -3.11 -m args.foundry.workspace_apply_patch_v0 --product-id `"$product_id`" --run-id `"$RunId`" --codegen `"$codegen_path`""
  $res = Invoke-Step -RunDir $run_dir -RunId $RunId -StepName "apply_patch" -CmdLine $cmd -EvidenceDir $EvidenceDir
  $apply = $res.stdout
  $apply_path = Join-Path $run_dir 'patch_report.json'
  $apply | Set-Content -Encoding utf8 $apply_path
  if ($res.rc -ne 0) { Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId $product_id -Step "apply_patch" -ExitCode $res.rc -EvidenceDir $EvidenceDir `
      -LastOutputPath $apply_path -LastStdout $apply -LastStdoutPath $res.stdout_path -LastStderrPath $res.stderr_path }
  Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "apply_patch" -Status "OK" -Ok $true -ExitCode 0 -Details @{
    output=$apply_path; stdout_path=$res.stdout_path; stderr_path=$res.stderr_path
  }

  $apply_obj = $apply | ConvertFrom-Json
  $ws = [string]$apply_obj.workspace

  # 2) Gate workspace
  $cmd = "py -3.11 -m args.foundry.workspace_gate_v0 --workspace `"$ws`""
  $res = Invoke-Step -RunDir $run_dir -RunId $RunId -StepName "workspace_gate" -CmdLine $cmd -EvidenceDir $EvidenceDir
  $gate = $res.stdout
  $gate_path = Join-Path $run_dir 'workspace_gate.json'
  $gate | Set-Content -Encoding utf8 $gate_path
  if ($res.rc -ne 0) { Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId $product_id -Step "workspace_gate" -ExitCode $res.rc -EvidenceDir $EvidenceDir `
      -LastOutputPath $gate_path -LastStdout $gate -LastStdoutPath $res.stdout_path -LastStderrPath $res.stderr_path }
  Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "workspace_gate" -Status "OK" -Ok $true -ExitCode 0 -Details @{
    output=$gate_path; stdout_path=$res.stdout_path; stderr_path=$res.stderr_path; workspace=$ws
  }

  # 3) Build EXE
  $out_dir = Join-Path $repoResolved ("dist\" + $product_id)
  $cmd = "py -3.11 -m args.foundry.build_exe_v0 --product-id `"$product_id`" --workspace `"$ws`" --entrypoint src/main.py --out-dir `"$out_dir`""
  $res = Invoke-Step -RunDir $run_dir -RunId $RunId -StepName "build_exe" -CmdLine $cmd -EvidenceDir $EvidenceDir
  $build = $res.stdout
  $build_path = Join-Path $run_dir 'build_exe.json'
  $build | Set-Content -Encoding utf8 $build_path
  if ($res.rc -ne 0) { Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId $product_id -Step "build_exe" -ExitCode $res.rc -EvidenceDir $EvidenceDir `
      -LastOutputPath $build_path -LastStdout $build -LastStdoutPath $res.stdout_path -LastStderrPath $res.stderr_path }
  Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "build_exe" -Status "OK" -Ok $true -ExitCode 0 -Details @{
    output=$build_path; stdout_path=$res.stdout_path; stderr_path=$res.stderr_path; out_dir=$out_dir
  }

  # 4) Acceptance on DIR (so it gets embedded into zip)
  $acceptance_ok = $false
  $acceptance_report_path = (Join-Path $run_dir "acceptance_gate.json")
  $acceptance_dist_path = (Join-Path $out_dir "acceptance_gate.json")

  if ($RunAcceptance -eq "YES") {
    $cmd = "py -3.11 -m args.foundry.acceptance_gate_v1 --dir `"$out_dir`" --out `"$acceptance_report_path`""
    $res = Invoke-Step -RunDir $run_dir -RunId $RunId -StepName "acceptance_gate" -CmdLine $cmd -EvidenceDir $EvidenceDir
    $acc = $res.stdout
    $acc | Set-Content -Encoding utf8 $acceptance_report_path

    if ($res.rc -ne 0) {
      Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId $product_id -Step "acceptance_gate" -ExitCode $res.rc -EvidenceDir $EvidenceDir `
        -LastOutputPath $acceptance_report_path -LastStdout $acc -LastStdoutPath $res.stdout_path -LastStderrPath $res.stderr_path
    }

    try { $acc_obj = $acc | ConvertFrom-Json; $acceptance_ok = [bool]$acc_obj.ok } catch { $acceptance_ok = $false }
    if (-not $acceptance_ok) {
      Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId $product_id -Step "acceptance_gate" -ExitCode $RC_FAIL -EvidenceDir $EvidenceDir `
        -LastOutputPath $acceptance_report_path -LastStdout $acc -LastStdoutPath $res.stdout_path -LastStderrPath $res.stderr_path
    }

    # copy acceptance artifacts into dist/<product> so release_pack includes them
    try {
      Copy-Item -Force $acceptance_report_path $acceptance_dist_path
      Copy-Item -Force $res.stdout_path (Join-Path $out_dir "acceptance_gate.stdout.txt")
      Copy-Item -Force $res.stderr_path (Join-Path $out_dir "acceptance_gate.stderr.txt")
    } catch { }

    Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "acceptance_gate" -Status "OK" -Ok $true -ExitCode 0 -Details @{
      output=$acceptance_report_path; embedded=$acceptance_dist_path; out_dir=$out_dir
      stdout_path=$res.stdout_path; stderr_path=$res.stderr_path
    }
  } else {
    Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "acceptance_gate" -Status "SKIP" -Ok $true -ExitCode 0 -Details @{
      reason="RunAcceptance=NO"; out_dir=$out_dir
    }
  }

  # 5) Release pack (now includes acceptance files)
  $cmd = "py -3.11 -m args.foundry.release_pack_v0 --product-id `"$product_id`""
  $res = Invoke-Step -RunDir $run_dir -RunId $RunId -StepName "release_pack" -CmdLine $cmd -EvidenceDir $EvidenceDir
  $rel = $res.stdout
  $rel_path = Join-Path $run_dir 'release_pack.json'
  $rel | Set-Content -Encoding utf8 $rel_path
  if ($res.rc -ne 0) { Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId $product_id -Step "release_pack" -ExitCode $res.rc -EvidenceDir $EvidenceDir `
      -LastOutputPath $rel_path -LastStdout $rel -LastStdoutPath $res.stdout_path -LastStderrPath $res.stderr_path }
  Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "release_pack" -Status "OK" -Ok $true -ExitCode 0 -Details @{
    output=$rel_path; stdout_path=$res.stdout_path; stderr_path=$res.stderr_path
  }

  $rel_obj = $rel | ConvertFrom-Json
  $rid = [string]$rel_obj.release_id
  $release_zip = [string]$rel_obj.release_zip
  $release_hashes = [string]$rel_obj.release_hashes

  # 6) Verify release
  $cmd = "py -3.11 -m args.foundry.release_verify_v0 --release-id `"$rid`""
  $res = Invoke-Step -RunDir $run_dir -RunId $RunId -StepName "release_verify" -CmdLine $cmd -EvidenceDir $EvidenceDir
  $ver = $res.stdout
  $ver_path = Join-Path $run_dir 'release_verify.json'
  $ver | Set-Content -Encoding utf8 $ver_path
  if ($res.rc -ne 0) { Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId $product_id -Step "release_verify" -ExitCode $res.rc -EvidenceDir $EvidenceDir `
      -LastOutputPath $ver_path -LastStdout $ver -LastStdoutPath $res.stdout_path -LastStderrPath $res.stderr_path }
  Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "release_verify" -Status "OK" -Ok $true -ExitCode 0 -Details @{
    output=$ver_path; stdout_path=$res.stdout_path; stderr_path=$res.stderr_path; release_id=$rid
  }

  # 7) Final report
  $final = [ordered]@{
    schema            = 'factory_final_report_v1'
    ok                = $true
    exit_code         = 0
    run_id            = $RunId
    product_id        = $product_id
    run_dir           = $run_dir
    release_id        = $rid
    release_zip       = $release_zip
    release_hashes    = $release_hashes
    evidence_dir      = $EvidenceDir
    events_jsonl      = (Join-Path $run_dir "events.jsonl")
    final_report_json = (Join-Path $run_dir "final_report.json")
    acceptance_ok     = $acceptance_ok
    acceptance_report = $(if ($RunAcceptance -eq "YES") { $acceptance_report_path } else { "" })
    acceptance_embedded = $(if ($RunAcceptance -eq "YES") { $acceptance_dist_path } else { "" })
  }

  Write-FinalReportFile -RunDir $run_dir -Report $final

  Write-RunEvent -RunDir $run_dir -RunId $RunId -Step "build_release" -Status "OK" -Ok $true -ExitCode 0 -Details @{
    release_id=$rid; release_zip=$release_zip; acceptance_ok=$acceptance_ok; run_acceptance=$RunAcceptance
  }

  ($final | ConvertTo-Json -Compress -Depth 30)
  exit 0
}
catch {
  $msg = $_.Exception.Message

  # If we have a valid run_dir, write evidence + final_report, then emit exactly one JSON.
  if ($run_dir -and (Test-Path -Path $run_dir -PathType Container)) {
    if (-not $EvidenceDir) { $EvidenceDir = Join-Path $run_dir "evidence" }
    Ensure-Dir $EvidenceDir

    $errPath = Join-Path $EvidenceDir "unhandled_exception.stderr.txt"
    ($msg | Out-String) | Set-Content -Encoding utf8 -Path $errPath

    $errObj = [ordered]@{
      schema    = "factory_unhandled_exception_v1"
      ts_utc    = (New-UtcIso)
      ok        = $false
      exit_code = $RC_INFRA
      run_id    = $RunId
      run_dir   = $run_dir
      error     = $msg
    }
    $errJson = ($errObj | ConvertTo-Json -Compress -Depth 30)
    $errOut  = Join-Path $run_dir "unhandled_exception.json"
    $errJson | Set-Content -Encoding utf8 -Path $errOut

    Fail-And-Exit -RunDir $run_dir -RunId $RunId -ProductId $product_id -Step "unhandled_exception" -ExitCode $RC_INFRA -EvidenceDir $EvidenceDir `
      -LastOutputPath $errOut -LastStdout $errJson -LastStdoutPath "" -LastStderrPath $errPath
  }

  # Fallback (no run_dir): still one JSON stdout
  ($([ordered]@{
    schema="factory_final_report_v1"
    ok=$false
    exit_code=$RC_INFRA
    run_id=$RunId
    failed_step="unhandled_exception"
    error=$msg
  }) | ConvertTo-Json -Compress -Depth 10)
  exit $RC_INFRA
}
finally {
  try { Pop-Location } catch { }
}
