
param(
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$IncludeChaos = "YES",
  [Parameter(Mandatory=$false)][string]$MatrixPath = "manifests\family_suite_matrix_v1.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exit codes (project standard)
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

# ---------- UTF-8 no-BOM writers (factory standard) ----------
function Write-TextUtf8NoBom([string]$Path, [string]$Text) {
  Ensure-Dir (Split-Path -Parent $Path)
  $enc = New-Object System.Text.UTF8Encoding $false
  [IO.File]::WriteAllText($Path, $Text, $enc)
}

function Append-TextUtf8NoBom([string]$Path, [string]$Text) {
  Ensure-Dir (Split-Path -Parent $Path)
  $enc = New-Object System.Text.UTF8Encoding $false
  [IO.File]::AppendAllText($Path, $Text, $enc)
}

function Write-JsonAtomic([string]$Path, [object]$Obj) {
  Ensure-Dir (Split-Path -Parent $Path)
  $tmp = "$Path.tmp"
  $json = ($Obj | ConvertTo-Json -Compress -Depth 80)
  $enc = New-Object System.Text.UTF8Encoding $false
  [IO.File]::WriteAllText($tmp, $json, $enc)
  Move-Item -Force -Path $tmp -Destination $Path
}

function Append-Event([string]$EventsPath, [string]$RunId, [string]$Kind, [hashtable]$Data) {
  $ev = [ordered]@{
    schema = "event_v0"
    ts_utc = UtcNowIso
    run_id = $RunId
    step   = "family_test_suite_v1"
    kind   = $Kind
    data   = $Data
  }
  $line = (($ev | ConvertTo-Json -Compress -Depth 80) + "`n")
  Append-TextUtf8NoBom $EventsPath $line
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
  # Read JSON tolerating UTF-8 BOM
  $raw = Get-Content -Raw -Encoding utf8 -Path $Path
  if ($null -eq $raw) { $raw = "" }
  $raw = ($raw -replace "^\uFEFF","")
  return ($raw | ConvertFrom-Json -ErrorAction Stop)
}

function Sanitize-Token([string]$s) {
  if ($null -eq $s) { return "null" }
  return ($s -replace '[^A-Za-z0-9_\-]+','_')
}

# ---------- Positive E2E run_id/release_id extraction (Stage 1D strict) ----------
function Extract-RunMeta([object]$J) {
  $rid = ""
  $rel = ""

  # Primary
  try { $rid = [string]$J.run_id } catch { $rid = "" }
  try { $rel = [string]$J.release_id } catch { $rel = "" }

  # Fallback: wrapper keeps IDs under build_release.*
  if ([string]::IsNullOrWhiteSpace($rid)) {
    try { $rid = [string]$J.build_release.run_id } catch { $rid = "" }
  }
  if ([string]::IsNullOrWhiteSpace($rel)) {
    try { $rel = [string]$J.build_release.release_id } catch { $rel = "" }
  }

  # Defensive: rare double nesting
  if ([string]::IsNullOrWhiteSpace($rid)) {
    try { $rid = [string]$J.build_release.build_release.run_id } catch { $rid = "" }
  }
  if ([string]::IsNullOrWhiteSpace($rel)) {
    try { $rel = [string]$J.build_release.build_release.release_id } catch { $rel = "" }
  }

  return [pscustomobject]@{ run_id = $rid; release_id = $rel }
}

# ---------- BOM scrub (Stage 1D) ----------
function Remove-Utf8BomIfPresent([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
  try {
    $b = [IO.File]::ReadAllBytes($Path)
    if ($b.Length -ge 3 -and $b[0] -eq 0xEF -and $b[1] -eq 0xBB -and $b[2] -eq 0xBF) {
      $nb = New-Object byte[] ($b.Length - 3)
      [Array]::Copy($b, 3, $nb, 0, $nb.Length)
      [IO.File]::WriteAllBytes($Path, $nb)
      return $true
    }
  } catch {}
  return $false
}

function Scrub-RunDirUtf8Bom([string]$RunDir) {
  $ts = UtcNowIso
  $scanned = 0
  $changed = 0
  $errors = @()

  if (-not (Test-Path -LiteralPath $RunDir -PathType Container)) {
    return [ordered]@{ schema="bom_scrub_v1"; ts_utc=$ts; ok=$false; exit_code=2; run_dir=$RunDir; scanned=0; changed=0; errors=@("run_dir_missing") }
  }

  try {
    $files = Get-ChildItem -LiteralPath $RunDir -Recurse -File -Include *.json,*.jsonl -ErrorAction SilentlyContinue
    foreach ($f in $files) {
      $scanned++
      if (Remove-Utf8BomIfPresent $f.FullName) { $changed++ }
    }
    return [ordered]@{ schema="bom_scrub_v1"; ts_utc=$ts; ok=$true; exit_code=0; run_dir=$RunDir; scanned=$scanned; changed=$changed; errors=@() }
  } catch {
    $errors += $_.Exception.Message
    return [ordered]@{ schema="bom_scrub_v1"; ts_utc=$ts; ok=$false; exit_code=2; run_dir=$RunDir; scanned=$scanned; changed=$changed; errors=$errors }
  }
}

function Run-ContractGate([string]$SuiteEvidenceDir, [string]$Label, [string]$RunDir) {
  Ensure-Dir $SuiteEvidenceDir
  $outPath = Join-Path $SuiteEvidenceDir $Label

  # paired scrub report path
  $scrubPath = ($outPath -replace 'contract_gate','bom_scrub')
  if ($scrubPath -eq $outPath) { $scrubPath = ($outPath + ".bom_scrub.json") }

  # 1) scrub BOM in target run_dir
  $scr = Scrub-RunDirUtf8Bom $RunDir
  Write-TextUtf8NoBom $scrubPath (($scr | ConvertTo-Json -Compress -Depth 10))

  if ([int]$scr.exit_code -ne 0) {
    # scrub failed -> INFRA
    Write-TextUtf8NoBom $outPath ""
    return [ordered]@{
      rc=2; rc_raw=2; ok=$false; infra=$true; parsed_ok=$false;
      json_path=$outPath; run_dir=$RunDir;
      bom_scrub_json=$scrubPath; bom_scrub_scanned=$scr.scanned; bom_scrub_changed=$scr.changed
    }
  }

  # 2) run contract gate in STRICT mode (no BOM allowed)
  $outLines = @()
  $rc_raw = 2
  try {
    $outLines = & py -3.11 -m args.foundry.contract_gate_v1 --run-dir $RunDir --events-parse YES --bom-strict YES 2>$null
    $rc_raw = $LASTEXITCODE
  } catch {
    $outLines = @()
    $rc_raw = 2
  }

  $rc = Normalize-Exit $rc_raw
  $txt = ($outLines -join "`n")
  Write-TextUtf8NoBom $outPath $txt

  $parsed_ok = $false
  try { $null = ($txt | ConvertFrom-Json -ErrorAction Stop); $parsed_ok = $true } catch {}

  $infra = $false
  if (-not $parsed_ok) { $infra = $true; $rc = 2 }

  return [ordered]@{
    rc=$rc; rc_raw=$rc_raw; ok=($rc -eq 0); infra=$infra; parsed_ok=$parsed_ok;
    json_path=$outPath; run_dir=$RunDir;
    bom_scrub_json=$scrubPath; bom_scrub_scanned=$scr.scanned; bom_scrub_changed=$scr.changed
  }
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
    Write-TextUtf8NoBom $stderrPath ("script_not_found: " + $scriptPath)
    Write-TextUtf8NoBom $stdoutPath ""
    return [ordered]@{
      infra=$true; rc=2; rc_raw=2;
      stdout_path=$stdoutPath; stderr_path=$stderrPath;
      stdout=""; json=$null; error=("script_not_found: " + $scriptPath)
    }
  }

  $outText = ""
  $rc_raw = 2

  try {
    Push-Location -Path $RepoPath
    try {
      $outLines = & powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath @Args 2> $stderrPath
      $rc_raw = $LASTEXITCODE
      $outText = ($outLines | Out-String)
    } finally {
      Pop-Location
    }
  } catch {
    $rc_raw = 2
    $outText = ""
    try { Write-TextUtf8NoBom $stderrPath (($_ | Out-String)) } catch {}
  }

  # Normalize evidence encodings to UTF-8 no BOM
  try { Write-TextUtf8NoBom $stdoutPath $outText } catch {}
  try {
    if (Test-Path -LiteralPath $stderrPath) {
      $errText = Get-Content -Raw -Path $stderrPath
      if ($null -eq $errText) { $errText = "" }
      Write-TextUtf8NoBom $stderrPath $errText
    } else {
      Write-TextUtf8NoBom $stderrPath ""
    }
  } catch {}

  $rc = Normalize-Exit $rc_raw
  $obj = Parse-OneJson $outText

  $infra = $false
  if ($null -eq $obj) { $infra = $true; $rc = 2; $rc_raw = 2 }

  return [ordered]@{
    infra=$infra; rc=$rc; rc_raw=$rc_raw;
    stdout_path=$stdoutPath; stderr_path=$stderrPath;
    stdout=$outText; json=$obj; error=""
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
    case_id=$CaseId; kit_id=$KitId; product_id=$ProductId;
    rc=$res.rc; rc_raw=$res.rc_raw; infra=$res.infra
  }

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

  $suiteRunDir   = Join-Path $repoPath ("args\data\runs\" + $suiteRunId)
  $suiteEvidence = Join-Path $suiteRunDir "evidence"
  $suiteEvents   = Join-Path $suiteRunDir "events.jsonl"
  $suiteFinal    = Join-Path $suiteRunDir "final_report.json"

  Ensure-Dir $suiteRunDir
  Ensure-Dir $suiteEvidence

  Append-Event $suiteEvents $suiteRunId "start" @{
    repo=$repoPath; suite_run_id=$suiteRunId;
    run_acceptance=$RunAcceptance; include_chaos=$IncludeChaos; matrix_path=$MatrixPath
  }

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

  # Save matrix_used.json as UTF-8 no-BOM (do not Copy-Item with BOM)
  $matrixUsed = Join-Path $suiteEvidence "matrix_used.json"
  Write-TextUtf8NoBom (($matrixUsed)) (($m | ConvertTo-Json -Compress -Depth 80))
  Append-Event $suiteEvents $suiteRunId "matrix_loaded" @{ path=$matrixAbs }

  $runIdByCase = @{}

  # ---------- Positive E2E ----------
  foreach ($c in @($m.positive_e2e)) {
    $caseId    = [string]$c.case_id
    $kitId     = [string]$c.kit_id
    $productId = [string]$c.product_id
    $summary   = ("FamilySuite v1 {0} {1}" -f $caseId, $productId)

    $res = Invoke-PsFile -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -CaseId $caseId `
      -RelScriptPath "scripts\run_factory_app_build_release_no_llm_v1.ps1" `
      -Args @("-KitId",$kitId,"-ProductId",$productId,"-Summary",$summary,"-RunAcceptance",$RunAcceptance)

    $accOk = $false
    $runId = ""
    $releaseId = ""
    $ok = $false

    if (-not $res.infra -and $res.rc -eq 0) {
      try { $accOk = [bool]$res.json.acceptance_ok } catch { $accOk = $false }

      $meta = Extract-RunMeta $res.json
      $runId = [string]$meta.run_id
      $releaseId = [string]$meta.release_id

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
  # ---------- Customization E2E ----------
  foreach ($c in @($m.customization_e2e)) {
    $caseId  = [string]$c.case_id
    $reqCase = [string]$c.requires_positive_case_id
    $reqPath = [string]$c.request_path

    $baseReleaseId = ""
    if ($releaseIdByCase.ContainsKey($reqCase)) { $baseReleaseId = [string]$releaseIdByCase[$reqCase] }

    if ([string]::IsNullOrWhiteSpace($baseReleaseId)) {
      $cases += [ordered]@{
        case_id=$caseId; kind="customization_e2e"; requires_positive_case_id=$reqCase;
        expected=@{ exit_code=0 };
        actual=@{ infra=$false; rc=1; rc_raw=1; run_id=""; release_id=""; base_release_id="" };
        ok=$false;
        evidence=@{ stdout_path=""; stderr_path="" };
        error=("missing_prereq_release_id: " + $reqCase)
      }
      Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$caseId; kind="customization_e2e"; ok=$false; error="missing_prereq_release_id" }
      continue
    }

    $reqAbs = $reqPath
    if (-not [System.IO.Path]::IsPathRooted($reqAbs)) { $reqAbs = Join-Path $repoPath $reqPath }

    $res = Invoke-PsFile -RepoPath $repoPath -SuiteEvidenceDir $suiteEvidence -CaseId $caseId `
      -RelScriptPath "scripts\run_factory_customize_release_v1.ps1" `
      -Args @("-Request",$reqAbs,"-BaseReleaseIdOverride",$baseReleaseId,"-Repo",$repoPath)

    $accOk = $false
    $runId = ""
    $releaseId = ""
    $ok = $false

    if (-not $res.infra -and $res.rc -eq 0) {
      try { $accOk = [bool]$res.json.acceptance_ok } catch { $accOk = $false }
      try { $runId = [string]$res.json.run_id } catch { $runId = "" }
      try { $releaseId = [string]$res.json.release_id } catch { $releaseId = "" }
      if (-not [string]::IsNullOrWhiteSpace($runId) -and -not [string]::IsNullOrWhiteSpace($releaseId)) { $ok = $true }
    }

    if ($res.infra -or $res.rc -eq 2) { $infraHit = $true }
    if ($ok) {
      $runIdByCase[$caseId] = $runId
      $releaseIdByCase[$caseId] = $releaseId
    }

    $cases += [ordered]@{
      case_id=$caseId; kind="customization_e2e"; requires_positive_case_id=$reqCase;
      expected=@{ exit_code=0 };
      actual=@{ infra=$res.infra; rc=$res.rc; rc_raw=$res.rc_raw; acceptance_ok=$accOk; run_id=$runId; release_id=$releaseId; base_release_id=$baseReleaseId };
      ok=$ok;
      evidence=@{ stdout_path=$res.stdout_path; stderr_path=$res.stderr_path };
      error=$res.error
    }

    Append-Event $suiteEvents $suiteRunId "case_done" @{ case_id=$caseId; kind="customization_e2e"; rc=$res.rc; infra=$res.infra; ok=$ok }
  }
  # ---------- Negative ----------
  foreach ($n in @($m.negative)) {
    $id        = [string]$n.case_id
    $prep      = [string]$n.prep
    $kitId     = [string]$n.kit_id
    $productId = [string]$n.product_id
    $summary   = ("FamilySuite v1 neg {0}" -f $id)

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

    $rid    = $p.run_id
    $runDir = Join-Path $repoPath ("args\data\runs\" + $rid)
    $jr     = Join-Path $runDir "job_request.json"
    $tmpl   = Join-Path $runDir "codegen_output.template.json"
    $out    = Join-Path $runDir "codegen_output.json"

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
        Write-TextUtf8NoBom $out "{INVALID_JSON"
      }
      elseif ($prep -eq "allowed_paths_block_all") {
        Copy-Item -Force $tmpl $out
        $jrText = Get-Content -Raw -Encoding utf8 -Path $jr
        if ($null -eq $jrText) { $jrText = "" }
        $jrText = ($jrText -replace "^\uFEFF","")
        $jrObj = $jrText | ConvertFrom-Json

        if ($null -eq $jrObj.allowed_paths) {
          $jrObj | Add-Member -NotePropertyName allowed_paths -NotePropertyValue @() -Force
        } else {
          $jrObj.allowed_paths = @()
        }

        $jrJson = ($jrObj | ConvertTo-Json -Depth 80)
        Write-TextUtf8NoBom $jr $jrJson
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
      $req    = [string]$z.requires_positive_case_id

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

  # ---------- Summary ----------
  $total  = $cases.Count
  $passed = @($cases | Where-Object { $_.ok -eq $true }).Count
  $failed = $total - $passed

  $suiteOk  = ($failed -eq 0) -and (-not $infraHit)
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

  # 1) Pre-write final_report
  Write-JsonAtomic $suiteFinal $final
  Append-Event $suiteEvents $suiteRunId "pre_final_written" @{
    ok=$suiteOk; exit_code=$exitCode; total=$total; passed=$passed; failed=$failed; infra=$infraHit
  }

  # 2) Contract Gate Pack (STRICT)
  $cg = [ordered]@{
    schema = "contract_gate_pack_v1"
    ok = $true
    exit_code = 0
    failed = 0
    infra = 0
    suite = $null
    cases = @()
    errors = @()
  }

  $cgSuite = Run-ContractGate -SuiteEvidenceDir $suiteEvidence -Label "contract_gate_suite.json" -RunDir $suiteRunDir
  $cg.suite = $cgSuite
  if ($cgSuite.rc -eq 2) { $cg.infra += 1; $cg.exit_code = 2 }
  elseif ($cgSuite.rc -eq 1) { $cg.failed += 1; if ($cg.exit_code -ne 2) { $cg.exit_code = 1 } }

  foreach ($c in $cases) {
    $caseId = [string]$c.case_id
    $rid = ""
    try { $rid = [string]$c.actual.run_id } catch { $rid = "" }

    if ([string]::IsNullOrWhiteSpace($rid)) {
      $cg.cases += [ordered]@{ case_id=$caseId; run_id=""; rc=2; ok=$false; infra=$true; json_path=""; error="missing_case_run_id" }
      $cg.infra += 1
      $cg.exit_code = 2
      continue
    }

    $runDir = Join-Path $repoPath ("args\data\runs\" + $rid)
    $fname  = "contract_gate_case_" + (Sanitize-Token $caseId) + ".json"
    $r = Run-ContractGate -SuiteEvidenceDir $suiteEvidence -Label $fname -RunDir $runDir

    $cg.cases += [ordered]@{
      case_id=$caseId; run_id=$rid; rc=$r.rc; ok=$r.ok; infra=$r.infra;
      json_path=$r.json_path; run_dir=$runDir;
      bom_scrub_json=$r.bom_scrub_json; bom_scrub_scanned=$r.bom_scrub_scanned; bom_scrub_changed=$r.bom_scrub_changed
    }

    if ($r.rc -eq 2) { $cg.infra += 1; $cg.exit_code = 2 }
    elseif ($r.rc -eq 1) { $cg.failed += 1; if ($cg.exit_code -ne 2) { $cg.exit_code = 1 } }
  }

  $cg.ok = ($cg.exit_code -eq 0)
  $final.contract_gate = $cg

  # 3) Enforce: contract gate overrides suite result
  if ($cg.exit_code -ne 0) {
    $final.ok = $false
    $final.exit_code = [int]([Math]::Max([int]$final.exit_code, [int]$cg.exit_code))
  }

  # 4) Rewrite final + done event + emit ONE JSON
  Write-JsonAtomic $suiteFinal $final
  Append-Event $suiteEvents $suiteRunId "done" @{
    ok=$final.ok; exit_code=$final.exit_code; total=$total; passed=$passed; failed=$failed; infra=$infraHit;
    contract_gate_exit_code=$cg.exit_code; contract_gate_failed=$cg.failed; contract_gate_infra=$cg.infra
  }

  Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
  exit ([int]$final.exit_code)
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
