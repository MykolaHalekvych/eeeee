param(
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$IncludeChaos = "YES"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) }

function RandHex([int]$n) {
  -join (1..$n | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
}

function Ensure-Dir([string]$p) {
  if (-not (Test-Path -LiteralPath $p -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $p | Out-Null
  }
}

function Normalize-Exit([object]$v) {
  try { $n = [int]$v } catch { return $RC_INFRA }
  if ($n -eq 0 -or $n -eq 1 -or $n -eq 2) { return $n }
  return $RC_INFRA
}

function Write-Text([string]$Path, [string]$Text) {
  Ensure-Dir (Split-Path -Parent $Path)
  Set-Content -Encoding utf8 -Path $Path -Value $Text
}

function Write-JsonAtomic([string]$Path, [object]$Obj) {
  Ensure-Dir (Split-Path -Parent $Path)
  $tmp = "$Path.tmp"
  ($Obj | ConvertTo-Json -Compress -Depth 50) | Set-Content -Encoding utf8 -Path $tmp
  Move-Item -Force -Path $tmp -Destination $Path
}

function Append-Event([string]$EventsPath, [string]$RunId, [string]$Kind, [hashtable]$Data) {
  $ev = [ordered]@{
    schema = "event_v0"
    ts_utc = UtcNowIso
    run_id = $RunId
    step = "family_test_suite_v1"
    kind = $Kind
    data = $Data
  }
  Ensure-Dir (Split-Path -Parent $EventsPath)
  Add-Content -Encoding utf8 -Path $EventsPath -Value ((($ev | ConvertTo-Json -Compress -Depth 50) + "`n"))
}

function Parse-OneJson([string]$Raw) {
  if ($null -eq $Raw) { return $null }
  $s = $Raw.Trim()
  if ($s -eq "") { return $null }

  try { return ($s | ConvertFrom-Json -ErrorAction Stop) } catch {}

  $lines = $s -split "`r?`n"
  for ($i = $lines.Length - 1; $i -ge 0; $i--) {
    $c = $lines[$i].Trim()
    if ($c.StartsWith("{")) {
      try { return ($c | ConvertFrom-Json -ErrorAction Stop) } catch {}
    }
  }
  return $null
}

function Invoke-PsFile {
  param(
    [Parameter(Mandatory=$true)][string]$RepoPath,
    [Parameter(Mandatory=$true)][string]$SuiteEvidenceDir,
    [Parameter(Mandatory=$true)][string]$CaseId,
    [Parameter(Mandatory=$true)][string]$RelScriptPath,
    [Parameter(Mandatory=$false)][string[]]$Args = @()
  )

  $scriptPath = Join-Path $RepoPath $RelScriptPath
  if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    return [ordered]@{
      infra = $true
      rc = $RC_INFRA
      rc_raw = 2
      stdout_path = ""
      stderr_path = ""
      stdout = ""
      json = $null
      error = "script_not_found: $scriptPath"
    }
  }

  $stdoutPath = Join-Path $SuiteEvidenceDir ("{0}.stdout.txt" -f $CaseId)
  $stderrPath = Join-Path $SuiteEvidenceDir ("{0}.stderr.txt" -f $CaseId)
  if (Test-Path -LiteralPath $stdoutPath) { Remove-Item -Force $stdoutPath }
  if (Test-Path -LiteralPath $stderrPath) { Remove-Item -Force $stderrPath }

  Ensure-Dir $SuiteEvidenceDir

  $outText = ""
  $rc_raw = 2

  try {
    Push-Location -Path $RepoPath
    try {
      # Важно: аргументы передаются как объекты -> символ '|' не ломает парсинг командной строки
      $outLines = & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath @Args 2> $stderrPath
      $rc_raw = $LASTEXITCODE
      $outText = ($outLines | Out-String)
    } finally {
      Pop-Location
    }
  } catch {
    $rc_raw = 2
    $outText = ""
    try { ($_ | Out-String) | Set-Content -Encoding utf8 -Path $stderrPath } catch {}
  }

  try { Set-Content -Encoding utf8 -Path $stdoutPath -Value $outText } catch {}

  $rc = Normalize-Exit $rc_raw
  $obj = Parse-OneJson $outText

  return [ordered]@{
    infra = $false
    rc = $rc
    rc_raw = $rc_raw
    stdout_path = $stdoutPath
    stderr_path = $stderrPath
    stdout = $outText
    json = $obj
    error = ""
  }
}

  $stdoutPath = Join-Path $SuiteEvidenceDir ("{0}.stdout.txt" -f $CaseId)
  $stderrPath = Join-Path $SuiteEvidenceDir ("{0}.stderr.txt" -f $CaseId)
  if (Test-Path -LiteralPath $stdoutPath) { Remove-Item -Force $stdoutPath }
  if (Test-Path -LiteralPath $stderrPath) { Remove-Item -Force $stderrPath }

  Ensure-Dir $SuiteEvidenceDir

  $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $scriptPath) + $Args

  $p = Start-Process -FilePath "powershell" -WorkingDirectory $RepoPath `
        -ArgumentList $argList -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

  $rc_raw = [int]$p.ExitCode
  $rc = Normalize-Exit $rc_raw

  $outText = ""
  try { $outText = Get-Content -Raw -Encoding utf8 -Path $stdoutPath } catch { $outText = "" }
  if ($null -eq $outText) { $outText = "" }

  $obj = Parse-OneJson $outText

  return [ordered]@{
    infra = $false
    rc = $rc
    rc_raw = $rc_raw
    stdout_path = $stdoutPath
    stderr_path = $stderrPath
    stdout = $outText
    json = $obj
    error = ""
  }
}

function Prompt-CreateRun {
  param(
    [string]$RepoPath,[string]$SuiteEvidenceDir,[string]$SuiteEvents,[string]$SuiteRunId,
    [string]$KitId,[string]$ProductId,[string]$Summary,[string]$CaseId
  )
  $res = Invoke-PsFile -RepoPath $RepoPath -SuiteEvidenceDir $SuiteEvidenceDir -CaseId $CaseId `
    -RelScriptPath "scripts\run_factory_app_prompt_v2.ps1" `
    -Args @("-KitId",$KitId,"-ProductId",$ProductId,"-Summary",$Summary)

  Append-Event $SuiteEvents $SuiteRunId "prompt_done" @{
    case_id=$CaseId; kit_id=$KitId; product_id=$ProductId; rc=$res.rc; rc_raw=$res.rc_raw; infra=$res.infra; stdout_path=$res.stdout_path; stderr_path=$res.stderr_path
  }

  if ($res.infra -or $null -eq $res.json) { return [ordered]@{ ok=$false; run_id=""; res=$res } }

  $runId = ""
  try { $runId = [string]$res.json.run_id } catch { $runId = "" }
  if ([string]::IsNullOrWhiteSpace($runId)) { return [ordered]@{ ok=$false; run_id=""; res=$res } }

  return [ordered]@{ ok=$true; run_id=$runId; res=$res }
}

# ---------------- MAIN ----------------

$repoPath = ""
$suiteRunId = (UtcNowId) + "_" + (RandHex 8)
$suiteRunDir = ""
$suiteEvidence = ""
$suiteEvents = ""
$suiteFinal = ""

$cases = @()
$infraHit = $false

try {
  try { $repoPath = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { $repoPath = $Repo }

  $suiteRunDir = Join-Path $repoPath ("args\data\runs\" + $suiteRunId)
  $suiteEvidence = Join-Path $suiteRunDir "evidence"
  $suiteEvents = Join-Path $suiteRunDir "events.jsonl"
  $suiteFinal = Join-Path $suiteRunDir "final_report.json"

  Ensure-Dir $suiteEvidence
  Ensure-Dir $suiteRunDir

  Append-Event $suiteEvents $suiteRunId "start" @{
    repo=$repoPath; suite_run_id=$suiteRunId; run_acceptance=$RunAcceptance; include_chaos=$IncludeChaos
  }

  # ---------- Positive E2E (no-LLM) ----------
  $pos = @(
    @{ case_id="e2e_cli"; kit_id="kit_cli_tool_v1"; product_id="demo_cli_tool" },
    @{ case_id="e2e_service"; kit_id="kit_windows_service_v0"; product_id="windows_service_v0" },
    @{ case_id="e2e_dashboard"; kit_id="kit_web_dashboard_v0"; product_id="web_dashboard_v0" }
  )

  $serviceRunIdForChaos = ""

  foreach ($c in $pos) {
    $caseId = [string]$c.case_id
    $kitId = [string]$c.kit_id
    $productId = [string]$c.product_id
    $summary = ("FamilySuite v1 | {0} | {1}" -f $caseId, $productId)

    $res = Invoke-PsFile -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -CaseId $caseId `
      -RelScriptPath "scripts\run_factory_app_build_release_no_llm_v1.ps1" `
      -Args @("-KitId",$kitId,"-ProductId",$productId,"-Summary",$summary,"-RunAcceptance",$RunAcceptance)

    Append-Event $suiteEvents $suiteRunId "case_done" @{
      case_id=$caseId; kind="positive_e2e"; kit_id=$kitId; product_id=$productId; rc=$res.rc; rc_raw=$res.rc_raw; infra=$res.infra
    }

    $ok = $false
    $accOk = $false
    $runId = ""
    $releaseId = ""

    if (-not $res.infra -and $res.rc -eq 0 -and $null -ne $res.json) {
      try { $accOk = [bool]$res.json.acceptance_ok } catch { $accOk = $false }
      try { $runId = [string]$res.json.run_id } catch { $runId = "" }
      try { $releaseId = [string]$res.json.release_id } catch { $releaseId = "" }
      if ($accOk -eq $true) { $ok = $true }
    }

    if ($res.infra -or $res.rc -eq 2) { $infraHit = $true }

    if ($caseId -eq "e2e_service" -and $ok -and -not [string]::IsNullOrWhiteSpace($runId)) {
      $serviceRunIdForChaos = $runId
    }

    $cases += [ordered]@{
      case_id = $caseId
      kind = "positive_e2e"
      kit_id = $kitId
      product_id = $productId
      expected = @{ exit_code = 0; acceptance_ok = $true }
      actual = @{
        infra = $res.infra
        rc = $res.rc
        rc_raw = $res.rc_raw
        acceptance_ok = $accOk
        run_id = $runId
        release_id = $releaseId
      }
      ok = $ok
      evidence = @{ stdout_path=$res.stdout_path; stderr_path=$res.stderr_path }
      error = $res.error
    }
  }

  # ---------- Negative tests (build_release_v1 expected FAIL=1) ----------
  $negKit = "kit_windows_service_v0"
  $negProduct = "windows_service_v0"

  function Run-Negative {
    param(
      [string]$NegId,
      [string]$PrepKind
    )
    $summary = ("FamilySuite v1 | neg | {0}" -f $NegId)
    $p = Prompt-CreateRun -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -SuiteEvents $suiteEvents -SuiteRunId $suiteRunId `
      -KitId $negKit -ProductId $negProduct -Summary $summary -CaseId ("prep_" + $NegId)

    if (-not $p.ok) {
      $script:infraHit = $true
      return [ordered]@{
        ok=$false
        infra=$true
        run_id=""
        error="prep_prompt_failed"
        res=$p.res
      }
    }

    $rid = $p.run_id
    $runDir = Join-Path $repoPath ("args\data\runs\" + $rid)
    $jr = Join-Path $runDir "job_request.json"
    $tmpl = Join-Path $runDir "codegen_output.template.json"
    $out = Join-Path $runDir "codegen_output.json"

    try {
      if ($PrepKind -eq "missing_codegen") {
        # do nothing: codegen_output.json absent -> preflight should FAIL(1)
      }
      elseif ($PrepKind -eq "missing_job_request") {
        Copy-Item -Force $tmpl $out
        Remove-Item -Force $jr
      }
      elseif ($PrepKind -eq "invalid_codegen_json") {
        Copy-Item -Force $tmpl $out
        "{INVALID_JSON" | Set-Content -Encoding utf8 -Path $out
      }
      elseif ($PrepKind -eq "allowed_paths_block_all") {
        Copy-Item -Force $tmpl $out
        $jrText = Get-Content -Raw -Encoding utf8 $jr
        $jrObj = $jrText | ConvertFrom-Json
        if ($null -eq $jrObj.allowed_paths) {
          $jrObj | Add-Member -NotePropertyName allowed_paths -NotePropertyValue @() -Force
        } else {
          $jrObj.allowed_paths = @()
        }
        ($jrObj | ConvertTo-Json -Depth 50) | Set-Content -Encoding utf8 -Path $jr
      }
      else {
        throw "unknown_prep_kind: $PrepKind"
      }
    } catch {
      $script:infraHit = $true
      return [ordered]@{
        ok=$false; infra=$true; run_id=$rid; error=("prep_failed: " + $_.Exception.Message); res=$p.res
      }
    }

    $res = Invoke-PsFile -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -CaseId $NegId `
      -RelScriptPath "scripts\run_factory_app_build_release_v1.ps1" `
      -Args @("-RunId",$rid,"-RunAcceptance","NO")

    Append-Event $suiteEvents $suiteRunId "case_done" @{
      case_id=$NegId; kind="negative"; prep_kind=$PrepKind; target_run_id=$rid; rc=$res.rc; rc_raw=$res.rc_raw; infra=$res.infra
    }

    $expected = 1
    $ok = $false
    if (-not $res.infra -and $res.rc -eq $expected) { $ok = $true }
    if ($res.infra) { $script:infraHit = $true }

    return [ordered]@{
      ok=$ok
      infra=$res.infra
      run_id=$rid
      res=$res
    }
  }

  $negDefs = @(
    @{ id="neg_missing_codegen"; prep="missing_codegen" },
    @{ id="neg_missing_job_request"; prep="missing_job_request" },
    @{ id="neg_invalid_codegen_json"; prep="invalid_codegen_json" },
    @{ id="neg_allowed_paths_block_all"; prep="allowed_paths_block_all" }
  )

  foreach ($n in $negDefs) {
    $id = [string]$n.id
    $prep = [string]$n.prep
    $r = Run-Negative -NegId $id -PrepKind $prep

    $cases += [ordered]@{
      case_id = $id
      kind = "negative"
      expected = @{ exit_code = 1 }
      actual = @{
        infra = $r.infra
        rc = $(if ($r.res) { $r.res.rc } else { $RC_INFRA })
        rc_raw = $(if ($r.res) { $r.res.rc_raw } else { 2 })
        run_id = $r.run_id
      }
      ok = $r.ok
      evidence = @{
        stdout_path = $(if ($r.res) { $r.res.stdout_path } else { "" })
        stderr_path = $(if ($r.res) { $r.res.stderr_path } else { "" })
      }
      error = $(if ($r.res) { $r.res.error } else { $r.error })
    }
  }

  # ---------- Chaos P6 (optional) ----------
  if ($IncludeChaos -eq "YES") {
    $caseId = "chaos_file_lock_release_pack"
    if ([string]::IsNullOrWhiteSpace($serviceRunIdForChaos)) {
      $infraHit = $true
      $cases += [ordered]@{
        case_id=$caseId
        kind="chaos"
        expected=@{ exit_code=0; pass=@{ build_release_infra=$true; zip_unchanged=$true } }
        actual=@{ infra=$true; rc=2; rc_raw=2; run_id="" }
        ok=$false
        evidence=@{ stdout_path=""; stderr_path="" }
        error="missing_prereq: e2e_service run_id not available"
      }
      Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$caseId; kind="chaos"; infra=$true; error="missing_prereq" }
    } else {
      $res = Invoke-PsFile -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -CaseId $caseId `
        -RelScriptPath "scripts\run_chaos_file_lock_release_pack_v1.ps1" `
        -Args @("-RunId",$serviceRunIdForChaos,"-RestoreFinalReport","YES")

      $ok = $false
      $pass1 = $false
      $pass2 = $false
      if (-not $res.infra -and $res.rc -eq 0 -and $null -ne $res.json) {
        try { $pass1 = [bool]$res.json.pass.build_release_infra } catch { $pass1 = $false }
        try { $pass2 = [bool]$res.json.pass.zip_unchanged } catch { $pass2 = $false }
        if ($pass1 -and $pass2) { $ok = $true }
      }
      if ($res.infra) { $infraHit = $true }

      $cases += [ordered]@{
        case_id=$caseId
        kind="chaos"
        expected=@{ exit_code=0; pass=@{ build_release_infra=$true; zip_unchanged=$true } }
        actual=@{
          infra=$res.infra; rc=$res.rc; rc_raw=$res.rc_raw; run_id=$serviceRunIdForChaos
          pass=@{ build_release_infra=$pass1; zip_unchanged=$pass2 }
        }
        ok=$ok
        evidence=@{ stdout_path=$res.stdout_path; stderr_path=$res.stderr_path }
        error=$res.error
      }

      Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$caseId; kind="chaos"; rc=$res.rc; rc_raw=$res.rc_raw; infra=$res.infra; ok=$ok }
    }
  }

  # ---------- Final ----------
  $total = $cases.Count
  $passed = @($cases | Where-Object { $_.ok -eq $true }).Count
  $failed = $total - $passed

  $suiteOk = ($failed -eq 0) -and (-not $infraHit)
  $exitCode = $(if ($infraHit) { $RC_INFRA } elseif ($suiteOk) { $RC_OK } else { $RC_FAIL })

  $final = [ordered]@{
    schema = "family_test_suite_v1"
    ts_utc = UtcNowIso
    ok = $suiteOk
    exit_code = $exitCode
    repo = $repoPath
    suite_run_id = $suiteRunId
    suite_run_dir = $suiteRunDir
    evidence_dir = $suiteEvidence
    events_jsonl = $suiteEvents
    final_report_json = $suiteFinal
    config = @{
      run_acceptance = $RunAcceptance
      include_chaos = $IncludeChaos
    }
    summary = @{
      total = $total
      passed = $passed
      failed = $failed
      infra = $infraHit
    }
    cases = $cases
  }

  Write-JsonAtomic $suiteFinal $final
  Append-Event $suiteEvents $suiteRunId "done" @{ ok=$suiteOk; exit_code=$exitCode; total=$total; passed=$passed; failed=$failed; infra=$infraHit }

  # stdout: one JSON
  Write-Output ($final | ConvertTo-Json -Compress -Depth 50)
  exit $exitCode
}
catch {
  $msg = $_.Exception.Message

  # best-effort finalize
  $final = [ordered]@{
    schema = "family_test_suite_v1"
    ts_utc = UtcNowIso
    ok = $false
    exit_code = $RC_INFRA
    repo = $repoPath
    suite_run_id = $suiteRunId
    suite_run_dir = $suiteRunDir
    evidence_dir = $suiteEvidence
    events_jsonl = $suiteEvents
    final_report_json = $suiteFinal
    error = @{ kind="infra"; type="unhandled_exception"; message=$msg }
    summary = @{ total = $cases.Count; passed = @($cases | Where-Object { $_.ok -eq $true }).Count; failed = 0; infra = $true }
    cases = $cases
  }

  try { if ($suiteFinal) { Write-JsonAtomic $suiteFinal $final } } catch {}
  try { if ($suiteEvents) { Append-Event $suiteEvents $suiteRunId "error" @{ message=$msg } } } catch {}
  Write-Output ($final | ConvertTo-Json -Compress -Depth 50)
  exit $RC_INFRA
}
