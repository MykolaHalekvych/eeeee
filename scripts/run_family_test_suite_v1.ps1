param(
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$IncludeChaos = "YES",
  [Parameter(Mandatory=$false)][string]$MatrixPath = "manifests\family_suite_matrix_v1.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) }
function RandHex([int]$n) { -join (1..$n | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) }) }

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

function Write-JsonAtomic([string]$Path, [object]$Obj) {
  Ensure-Dir (Split-Path -Parent $Path)
  $tmp = "$Path.tmp"
  ($Obj | ConvertTo-Json -Compress -Depth 80) | Set-Content -Encoding utf8 -Path $tmp
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
  Add-Content -Encoding utf8 -Path $EventsPath -Value ((($ev | ConvertTo-Json -Compress -Depth 80) + "`n"))
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

function Read-JsonUtf8Sig([string]$Path) {
  $raw = Get-Content -Raw -Encoding utf8 -Path $Path
  if ($null -eq $raw) { $raw = "" }
  $raw = ($raw -replace "^\uFEFF","")
  return ($raw | ConvertFrom-Json -ErrorAction Stop)
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
  Ensure-Dir $SuiteEvidenceDir

  $stdoutPath = Join-Path $SuiteEvidenceDir ("{0}.stdout.txt" -f $CaseId)
  $stderrPath = Join-Path $SuiteEvidenceDir ("{0}.stderr.txt" -f $CaseId)
  if (Test-Path -LiteralPath $stdoutPath) { Remove-Item -Force $stdoutPath }
  if (Test-Path -LiteralPath $stderrPath) { Remove-Item -Force $stderrPath }

  if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    Set-Content -Encoding utf8 -Path $stderrPath -Value ("script_not_found: " + $scriptPath)
    Set-Content -Encoding utf8 -Path $stdoutPath -Value ""
    return [ordered]@{ infra=$true; rc=2; rc_raw=2; stdout_path=$stdoutPath; stderr_path=$stderrPath; stdout=""; json=$null; error=("script_not_found: " + $scriptPath) }
  }

  $outText = ""
  $rc_raw = 2

  try {
    Push-Location -Path $RepoPath
    try {
      # pipes-safe: args are passed as objects, not via cmdline string
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

  $infra = $false
  if ($null -eq $obj) { $infra = $true; $rc = 2; $rc_raw = 2 }

  return [ordered]@{ infra=$infra; rc=$rc; rc_raw=$rc_raw; stdout_path=$stdoutPath; stderr_path=$stderrPath; stdout=$outText; json=$obj; error="" }
}

function Prompt-CreateRun {
  param(
    [string]$RepoPath,[string]$SuiteEvidenceDir,[string]$SuiteEvents,[string]$SuiteRunId,
    [string]$KitId,[string]$ProductId,[string]$Summary,[string]$CaseId
  )

  $res = Invoke-PsFile -RepoPath $RepoPath -SuiteEvidenceDir $SuiteEvidenceDir -CaseId $CaseId `
    -RelScriptPath "scripts\run_factory_app_prompt_v2.ps1" `
    -Args @("-KitId",$KitId,"-ProductId",$ProductId,"-Summary",$Summary)

  Append-Event $SuiteEvents $SuiteRunId "prompt_done" @{ case_id=$CaseId; kit_id=$KitId; product_id=$ProductId; rc=$res.rc; rc_raw=$res.rc_raw; infra=$res.infra }

  if ($res.infra -or $res.rc -ne 0) { return [ordered]@{ ok=$false; run_id=""; res=$res } }

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

  Append-Event $suiteEvents $suiteRunId "start" @{ repo=$repoPath; suite_run_id=$suiteRunId; run_acceptance=$RunAcceptance; include_chaos=$IncludeChaos; matrix_path=$MatrixPath }

  $matrixAbs = $MatrixPath
  if (-not [System.IO.Path]::IsPathRooted($matrixAbs)) {
    $matrixAbs = Join-Path $repoPath $MatrixPath
  }

  if (-not (Test-Path -LiteralPath $matrixAbs -PathType Leaf)) {
    $final = [ordered]@{
      schema="family_test_suite_v1"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_FAIL
      repo=$repoPath
      suite_run_id=$suiteRunId
      suite_run_dir=$suiteRunDir
      evidence_dir=$suiteEvidence
      events_jsonl=$suiteEvents
      final_report_json=$suiteFinal
      error=@{ kind="fail"; type="missing_matrix"; message="Matrix file not found"; path=$matrixAbs }
      cases=@()
    }
    Write-JsonAtomic $suiteFinal $final
    Append-Event $suiteEvents $suiteRunId "error" @{ message="missing_matrix"; path=$matrixAbs }
    Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
    exit $RC_FAIL
  }

  $m = Read-JsonUtf8Sig $matrixAbs
  $mSchema = ""
  try { $mSchema = [string]$m.schema } catch { $mSchema = "" }
  if ($mSchema -ne "family_suite_matrix_v1") {
    $final = [ordered]@{
      schema="family_test_suite_v1"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_FAIL
      repo=$repoPath
      suite_run_id=$suiteRunId
      suite_run_dir=$suiteRunDir
      evidence_dir=$suiteEvidence
      events_jsonl=$suiteEvents
      final_report_json=$suiteFinal
      error=@{ kind="fail"; type="invalid_matrix_schema"; message="Expected schema=family_suite_matrix_v1"; got=$mSchema; path=$matrixAbs }
      cases=@()
    }
    Write-JsonAtomic $suiteFinal $final
    Append-Event $suiteEvents $suiteRunId "error" @{ message="invalid_matrix_schema"; got=$mSchema }
    Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
    exit $RC_FAIL
  }

  Copy-Item -Force $matrixAbs (Join-Path $suiteEvidence "matrix_used.json") | Out-Null
  Append-Event $suiteEvents $suiteRunId "matrix_loaded" @{ path=$matrixAbs }

  $runIdByCase = @{}

  # ---------- Positive E2E ----------
  foreach ($c in @($m.positive_e2e)) {
    $caseId = [string]$c.case_id
    $kitId = [string]$c.kit_id
    $productId = [string]$c.product_id
    $summary = ("FamilySuite v1 {0} {1}" -f $caseId, $productId)

    $res = Invoke-PsFile -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -CaseId $caseId `
      -RelScriptPath "scripts\run_factory_app_build_release_no_llm_v1.ps1" `
      -Args @("-KitId",$kitId,"-ProductId",$productId,"-Summary",$summary,"-RunAcceptance",$RunAcceptance)

    $accOk = $false
    $runId = ""
    $releaseId = ""
    $ok = $false

    if (-not $res.infra -and $res.rc -eq 0) {
      try { $accOk = [bool]$res.json.acceptance_ok } catch { $accOk = $false }
      try { $runId = [string]$res.json.run_id } catch { $runId = "" }
      try { $releaseId = [string]$res.json.release_id } catch { $releaseId = "" }
      if ($RunAcceptance -eq "NO") { $ok = $true } else { if ($accOk) { $ok = $true } }
    }

    if ($res.infra -or $res.rc -eq 2) { $infraHit = $true }
    if ($ok -and -not [string]::IsNullOrWhiteSpace($runId)) { $runIdByCase[$caseId] = $runId }

    $cases += [ordered]@{
      case_id=$caseId; kind="positive_e2e"; kit_id=$kitId; product_id=$productId;
      expected=@{ exit_code=0; acceptance_ok=$true };
      actual=@{ infra=$res.infra; rc=$res.rc; rc_raw=$res.rc_raw; acceptance_ok=$accOk; run_id=$runId; release_id=$releaseId };
      ok=$ok;
      evidence=@{ stdout_path=$res.stdout_path; stderr_path=$res.stderr_path };
      error=$res.error
    }

    Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$caseId; kind="positive_e2e"; rc=$res.rc; infra=$res.infra; ok=$ok }
  }

  # ---------- Negative ----------
  foreach ($n in @($m.negative)) {
    $id = [string]$n.case_id
    $prep = [string]$n.prep
    $kitId = [string]$n.kit_id
    $productId = [string]$n.product_id
    $summary = ("FamilySuite v1 neg {0}" -f $id)

    $p = Prompt-CreateRun -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -SuiteEvents $suiteEvents -SuiteRunId $suiteRunId `
      -KitId $kitId -ProductId $productId -Summary $summary -CaseId ("prep_" + $id)

    if (-not $p.ok) {
      $infraHit = $true
      $cases += [ordered]@{
        case_id=$id; kind="negative";
        expected=@{ exit_code=1 };
        actual=@{ infra=$true; rc=2; rc_raw=2; run_id="" };
        ok=$false;
        evidence=@{ stdout_path=$p.res.stdout_path; stderr_path=$p.res.stderr_path };
        error="prep_prompt_failed"
      }
      Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$id; kind="negative"; infra=$true; ok=$false; error="prep_prompt_failed" }
      continue
    }

    $rid = $p.run_id
    $runDir = Join-Path $repoPath ("args\data\runs\" + $rid)
    $jr = Join-Path $runDir "job_request.json"
    $tmpl = Join-Path $runDir "codegen_output.template.json"
    $out = Join-Path $runDir "codegen_output.json"

    try {
      if ($prep -eq "missing_codegen") {
        # keep codegen_output.json missing
      }
      elseif ($prep -eq "missing_job_request") {
        Copy-Item -Force $tmpl $out
        Remove-Item -Force $jr
      }
      elseif ($prep -eq "invalid_codegen_json") {
        Copy-Item -Force $tmpl $out
        "{INVALID_JSON" | Set-Content -Encoding utf8 -Path $out
      }
      elseif ($prep -eq "allowed_paths_block_all") {
        Copy-Item -Force $tmpl $out
        $jrText = Get-Content -Raw -Encoding utf8 $jr
        $jrObj = $jrText | ConvertFrom-Json
        if ($null -eq $jrObj.allowed_paths) {
          $jrObj | Add-Member -NotePropertyName allowed_paths -NotePropertyValue @() -Force
        } else {
          $jrObj.allowed_paths = @()
        }
        ($jrObj | ConvertTo-Json -Depth 80) | Set-Content -Encoding utf8 -Path $jr
      }
      else {
        throw "unknown_prep_kind: $prep"
      }
    } catch {
      $infraHit = $true
      $cases += [ordered]@{
        case_id=$id; kind="negative"; expected=@{ exit_code=1 };
        actual=@{ infra=$true; rc=2; rc_raw=2; run_id=$rid };
        ok=$false;
        evidence=@{ stdout_path=""; stderr_path="" };
        error=("prep_failed: " + $_.Exception.Message)
      }
      Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$id; kind="negative"; infra=$true; ok=$false; error="prep_failed" }
      continue
    }

    $res = Invoke-PsFile -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -CaseId $id `
      -RelScriptPath "scripts\run_factory_app_build_release_v1.ps1" `
      -Args @("-RunId",$rid,"-RunAcceptance","NO")

    $ok = ($res.infra -eq $false -and $res.rc -eq 1)
    if ($res.infra -or $res.rc -eq 2) { $infraHit = $true }

    $cases += [ordered]@{
      case_id=$id; kind="negative";
      expected=@{ exit_code=1 };
      actual=@{ infra=$res.infra; rc=$res.rc; rc_raw=$res.rc_raw; run_id=$rid };
      ok=$ok;
      evidence=@{ stdout_path=$res.stdout_path; stderr_path=$res.stderr_path };
      error=$res.error
    }

    Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$id; kind="negative"; rc=$res.rc; infra=$res.infra; ok=$ok }
  }

  # ---------- Chaos ----------
  if ($IncludeChaos -eq "YES") {
    foreach ($z in @($m.chaos)) {
      $caseId = [string]$z.case_id
      $req = [string]$z.requires_positive_case_id

      $runId = ""
      if ($runIdByCase.ContainsKey($req)) { $runId = [string]$runIdByCase[$req] }

      if ([string]::IsNullOrWhiteSpace($runId)) {
        $cases += [ordered]@{
          case_id=$caseId; kind="chaos";
          expected=@{ exit_code=0; pass=@{ build_release_infra=$true; zip_unchanged=$true } };
          actual=@{ infra=$false; rc=1; rc_raw=1; run_id="" };
          ok=$false;
          evidence=@{ stdout_path=""; stderr_path="" };
          error=("missing_prereq: " + $req + " run_id not available")
        }
        Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$caseId; kind="chaos"; ok=$false; error="missing_prereq" }
        continue
      }

      $res = Invoke-PsFile -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -CaseId $caseId `
        -RelScriptPath "scripts\run_chaos_file_lock_release_pack_v1.ps1" `
        -Args @("-RunId",$runId,"-RestoreFinalReport","YES")

      $pass1 = $false; $pass2 = $false; $ok = $false
      if (-not $res.infra -and $res.rc -eq 0) {
        try { $pass1 = [bool]$res.json.pass.build_release_infra } catch { $pass1 = $false }
        try { $pass2 = [bool]$res.json.pass.zip_unchanged } catch { $pass2 = $false }
        if ($pass1 -and $pass2) { $ok = $true }
      }

      if ($res.infra -or $res.rc -eq 2) { $infraHit = $true }

      $cases += [ordered]@{
        case_id=$caseId; kind="chaos";
        expected=@{ exit_code=0; pass=@{ build_release_infra=$true; zip_unchanged=$true } };
        actual=@{ infra=$res.infra; rc=$res.rc; rc_raw=$res.rc_raw; run_id=$runId; pass=@{ build_release_infra=$pass1; zip_unchanged=$pass2 } };
        ok=$ok;
        evidence=@{ stdout_path=$res.stdout_path; stderr_path=$res.stderr_path };
        error=$res.error
      }

      Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$caseId; kind="chaos"; rc=$res.rc; infra=$res.infra; ok=$ok }
    }
  }

  $total = $cases.Count
  $passed = @($cases | Where-Object { $_.ok -eq $true }).Count
  $failed = $total - $passed

  $suiteOk = ($failed -eq 0) -and (-not $infraHit)
  $exitCode = $(if ($infraHit) { $RC_INFRA } elseif ($suiteOk) { $RC_OK } else { $RC_FAIL })

  $final = [ordered]@{
    schema="family_test_suite_v1"
    ts_utc=UtcNowIso
    ok=$suiteOk
    exit_code=$exitCode
    repo=$repoPath
    suite_run_id=$suiteRunId
    suite_run_dir=$suiteRunDir
    evidence_dir=$suiteEvidence
    events_jsonl=$suiteEvents
    final_report_json=$suiteFinal
    config=@{ run_acceptance=$RunAcceptance; include_chaos=$IncludeChaos; matrix_path=$matrixAbs }
    summary=@{ total=$total; passed=$passed; failed=$failed; infra=$infraHit }
    cases=$cases
  }

  Write-JsonAtomic $suiteFinal $final
  Append-Event $suiteEvents $suiteRunId "done" @{ ok=$suiteOk; exit_code=$exitCode; total=$total; passed=$passed; failed=$failed; infra=$infraHit }

  Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
  exit $exitCode
}
catch {
  $msg = $_.Exception.Message
  $final = [ordered]@{
    schema="family_test_suite_v1"
    ts_utc=UtcNowIso
    ok=$false
    exit_code=$RC_INFRA
    repo=$repoPath
    suite_run_id=$suiteRunId
    suite_run_dir=$suiteRunDir
    evidence_dir=$suiteEvidence
    events_jsonl=$suiteEvents
    final_report_json=$suiteFinal
    error=@{ kind="infra"; type="unhandled_exception"; message=$msg }
    cases=$cases
  }
  try { if ($suiteFinal) { Write-JsonAtomic $suiteFinal $final } } catch {}
  try { if ($suiteEvents) { Append-Event $suiteEvents $suiteRunId "error" @{ message=$msg } } } catch {}
  Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
  exit $RC_INFRA
}
