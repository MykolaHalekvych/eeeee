param(
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path,
  [Parameter(Mandatory=$true)][int]$N,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$IncludeChaos = "YES",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
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

# ---------- UTF-8 no-BOM writers ----------
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
    step   = "suite_soak"
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

function Run-ContractGate([string]$EvidenceDir, [string]$Label, [string]$RunDir) {
  Ensure-Dir $EvidenceDir
  $outPath = Join-Path $EvidenceDir $Label

  # paired scrub report path
  $scrubPath = ($outPath -replace 'contract_gate','bom_scrub')
  if ($scrubPath -eq $outPath) { $scrubPath = ($outPath + ".bom_scrub.json") }

  # 1) scrub BOM in target run_dir
  $scr = Scrub-RunDirUtf8Bom $RunDir
  Write-TextUtf8NoBom $scrubPath (($scr | ConvertTo-Json -Compress -Depth 10))

  if ([int]$scr.exit_code -ne 0) {
    Write-TextUtf8NoBom $outPath ""
    return [ordered]@{
      rc=2; rc_raw=2; ok=$false; json_path=$outPath; run_dir=$RunDir;
      bom_scrub_json=$scrubPath; bom_scrub_scanned=$scr.scanned; bom_scrub_changed=$scr.changed
    }
  }

  # 2) strict contract gate (no BOM allowed)
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

  if (-not $parsed_ok) { $rc = 2 }

  return [ordered]@{
    rc=$rc; rc_raw=$rc_raw; ok=($rc -eq 0); json_path=$outPath; run_dir=$RunDir;
    bom_scrub_json=$scrubPath; bom_scrub_scanned=$scr.scanned; bom_scrub_changed=$scr.changed
  }
}

function Invoke-FamilySuiteOnce {
  param(
    [Parameter(Mandatory=$true)][string]$RepoPath,
    [Parameter(Mandatory=$true)][string]$EvidenceDir,
    [Parameter(Mandatory=$true)][int]$I,
    [Parameter(Mandatory=$true)][string]$RunAcceptance,
    [Parameter(Mandatory=$true)][string]$IncludeChaos,
    [Parameter(Mandatory=$true)][string]$MatrixPath
  )

  $caseTag = ("iter_{0:d2}" -f $I)
  $stdoutPath = Join-Path $EvidenceDir ($caseTag + ".suite.stdout.txt")
  $stderrPath = Join-Path $EvidenceDir ($caseTag + ".suite.stderr.txt")

  if (Test-Path -LiteralPath $stdoutPath) { Remove-Item -Force $stdoutPath }
  if (Test-Path -LiteralPath $stderrPath) { Remove-Item -Force $stderrPath }

  $outText = ""
  $rc_raw = 2
  $obj = $null
  $infra = $false

  try {
    Push-Location -Path $RepoPath
    try {
      $outLines = & powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_family_test_suite_v1.ps1" `
        -Repo $RepoPath -RunAcceptance $RunAcceptance -IncludeChaos $IncludeChaos -MatrixPath $MatrixPath 2> $stderrPath
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

  Write-TextUtf8NoBom $stdoutPath $outText
  if (Test-Path -LiteralPath $stderrPath) {
    $e = Get-Content -Raw -Path $stderrPath
    if ($null -eq $e) { $e = "" }
    Write-TextUtf8NoBom $stderrPath $e
  } else {
    Write-TextUtf8NoBom $stderrPath ""
  }

  $rc = Normalize-Exit $rc_raw
  $obj = Parse-OneJson $outText
  if ($null -eq $obj) { $infra = $true; $rc = 2; $rc_raw = 2 }

  return [ordered]@{
    rc=$rc; rc_raw=$rc_raw; infra=$infra;
    stdout_path=$stdoutPath; stderr_path=$stderrPath;
    json=$obj
  }
}

# ---------------- MAIN ----------------

$repoPath = ""
try { $repoPath = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { $repoPath = $Repo }

$runId     = (UtcNowId) + "_" + (RandHex 8)
$runDir    = Join-Path $repoPath ("args\data\runs\" + $runId)
$evidence  = Join-Path $runDir "evidence"
$events    = Join-Path $runDir "events.jsonl"
$finalPath = Join-Path $runDir "final_report.json"

Ensure-Dir $runDir
Ensure-Dir $evidence

Append-Event $events $runId "start" @{
  repo=$repoPath; run_id=$runId; n_target=$N; include_chaos=$IncludeChaos; run_acceptance=$RunAcceptance; matrix_path=$MatrixPath
}

$runs = @()
$firstFail = $null
$infraCount = 0
$failCount = 0

for ($i=1; $i -le $N; $i++) {
  Append-Event $events $runId "iter_start" @{ i=$i }

  $res = Invoke-FamilySuiteOnce -RepoPath $repoPath -EvidenceDir $evidence -I $i -RunAcceptance $RunAcceptance -IncludeChaos $IncludeChaos -MatrixPath $MatrixPath

  $suite = $res.json
  $suiteRunId = ""
  $suiteRunDir = ""
  $total = 0; $passed = 0; $failed = 0; $infra = $false
  $suiteExit = 2
  $tsSuite = UtcNowIso

  if ($res.infra -or $res.rc -eq 2) {
    $infra = $true
    $suiteExit = 2
  } elseif ($res.rc -eq 1) {
    $suiteExit = 1
  } else {
    try { $suiteRunId  = [string]$suite.suite_run_id } catch { $suiteRunId = "" }
    try { $suiteRunDir = [string]$suite.suite_run_dir } catch { $suiteRunDir = "" }
    try { $suiteExit   = [int]$suite.exit_code } catch { $suiteExit = 2 }
    try { $tsSuite     = [string]$suite.ts_utc } catch { $tsSuite = UtcNowIso }
    try { $total       = [int]$suite.summary.total } catch { $total = 0 }
    try { $passed      = [int]$suite.summary.passed } catch { $passed = 0 }
    try { $failed      = [int]$suite.summary.failed } catch { $failed = 0 }
    try { $infra       = [bool]$suite.summary.infra } catch { $infra = $false }
  }

  # STRICT contract gate on suite run_dir (scrub + --bom-strict YES)
  $cg = $null
  if (-not [string]::IsNullOrWhiteSpace($suiteRunDir)) {
    $cg = Run-ContractGate -EvidenceDir $evidence -Label ("iter_{0:d2}.contract_gate.json" -f $i) -RunDir $suiteRunDir
    if ($cg.rc -ne 0) {
      $suiteExit = [int]([Math]::Max($suiteExit, $cg.rc))
      $infra = ($cg.rc -eq 2) -or $infra
    }
  } else {
    $suiteExit = 2
    $infra = $true
    $cg = [ordered]@{ rc=2; rc_raw=2; ok=$false; json_path=""; run_dir=""; bom_scrub_json=""; bom_scrub_scanned=0; bom_scrub_changed=0 }
  }

  $okIter = ($suiteExit -eq 0)

  $runs += [ordered]@{
    i=$i
    exit_code=$suiteExit
    ok=$okIter
    suite_run_id=$suiteRunId
    total=$total
    passed=$passed
    failed=$failed
    infra=$infra
    ts_utc=$tsSuite
    contract_gate=$cg
  }

  if ($suiteExit -ne 0 -and $null -eq $firstFail) {
    $firstFail = [ordered]@{ i=$i; exit_code=$suiteExit; suite_run_id=$suiteRunId }
  }

  Append-Event $events $runId "iter_done" @{ i=$i; exit_code=$suiteExit; ok=$okIter; infra=$infra; suite_run_id=$suiteRunId }

  if ($suiteExit -eq 2) { $infraCount += 1 }
  elseif ($suiteExit -eq 1) { $failCount += 1 }
}

$exitCode = 0
if ($infraCount -gt 0) { $exitCode = 2 }
elseif ($failCount -gt 0) { $exitCode = 1 }
else { $exitCode = 0 }

$summary = [ordered]@{
  n_ran = $runs.Count
  ok = ($exitCode -eq 0)
  infra = $infraCount
  failed = $failCount
  n_target = $N
}

$final = [ordered]@{
  schema="suite_soak_v1"
  step="suite_soak"
  repo=$repoPath
  run_id=$runId
  run_dir=$runDir
  evidence_dir=$evidence
  events_jsonl=$events
  final_report_json=$finalPath
  ts_utc=UtcNowIso
  config=@{ include_chaos=$IncludeChaos; run_acceptance=$RunAcceptance; n_target=$N; matrix_path=$MatrixPath; bom_strict="YES" }
  summary=$summary
  first_fail=$firstFail
  runs=$runs
  ok=($exitCode -eq 0)
  exit_code=$exitCode
}

# Write final, then STRICT self contract gate (scrub + --bom-strict YES)
Write-JsonAtomic $finalPath $final

$cgSelf = Run-ContractGate -EvidenceDir $evidence -Label "contract_gate_self.json" -RunDir $runDir
$final.contract_gate_self = $cgSelf
if ($cgSelf.rc -ne 0) {
  $final.ok = $false
  $final.exit_code = [int]([Math]::Max([int]$final.exit_code, [int]$cgSelf.rc))
  $final.summary.ok = $false
  if ($cgSelf.rc -eq 2) { $final.summary.infra = [int]$final.summary.infra + 1 } else { $final.summary.failed = [int]$final.summary.failed + 1 }
}

Write-JsonAtomic $finalPath $final
Append-Event $events $runId "done" @{ ok=$final.ok; exit_code=$final.exit_code; n_ran=$final.summary.n_ran; infra=$final.summary.infra; failed=$final.summary.failed }

Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
exit ([int]$final.exit_code)
