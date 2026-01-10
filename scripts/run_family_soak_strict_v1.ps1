param(
  [Parameter(Mandatory=$true)][int]$N,
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$IncludeChaos = "YES",
  [Parameter(Mandatory=$false)][string]$MatrixPath = "manifests\family_suite_matrix_v1.json",
  [Parameter(Mandatory=$false)][string]$SuiteScript = "scripts\run_family_test_suite_v1.ps1",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$StopOnFirstFailure = "YES"
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
    step   = "family_soak_strict_v1"
    kind   = $Kind
    data   = $Data
  }
  $line = (($ev | ConvertTo-Json -Compress -Depth 80) + "`n")
  Append-TextUtf8NoBom $EventsPath $line
}

function Read-TextUtf8Sig([string]$Path) {
  # Read text tolerating UTF-8 BOM
  $raw = ""
  try { $raw = Get-Content -Raw -Encoding utf8 -Path $Path } catch { $raw = "" }
  if ($null -eq $raw) { $raw = "" }
  return ($raw -replace "^\uFEFF","")
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

function Run-ContractGate([string]$EvidenceDir, [string]$RunDir) {
  Ensure-Dir $EvidenceDir
  $outPath   = Join-Path $EvidenceDir "contract_gate_soak.json"
  $scrubPath = Join-Path $EvidenceDir "bom_scrub_soak.json"

  $scr = Scrub-RunDirUtf8Bom $RunDir
  Write-TextUtf8NoBom $scrubPath (($scr | ConvertTo-Json -Compress -Depth 10))

  if ([int]$scr.exit_code -ne 0) {
    Write-TextUtf8NoBom $outPath ""
    return [ordered]@{ rc=2; rc_raw=2; ok=$false; infra=$true; parsed_ok=$false; json_path=$outPath; bom_scrub_json=$scrubPath; bom_scrub_changed=$scr.changed; bom_scrub_scanned=$scr.scanned }
  }

  $outLines = @()
  $rc_raw = 2
  try {
    Push-Location -Path $Repo
    try {
      $outLines = & py -3.11 -m args.foundry.contract_gate_v1 --run-dir $RunDir --events-parse YES --bom-strict YES 2>$null
      $rc_raw = $LASTEXITCODE
    } finally {
      Pop-Location
    }
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
    json_path=$outPath; bom_scrub_json=$scrubPath; bom_scrub_changed=$scr.changed; bom_scrub_scanned=$scr.scanned
  }
}

# ---------------- MAIN ----------------
$repoPath = ""
$soakRunId = (UtcNowId) + "_" + (RandHex 8)
$soakRunDir = ""
$evidenceDir = ""
$eventsJsonl = ""
$finalJson = ""

$iters = @()
$infraHit = $false
$failHit = $false

try {
  try { $repoPath = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { $repoPath = $Repo }

  $suiteAbs = $SuiteScript
  if (-not [System.IO.Path]::IsPathRooted($suiteAbs)) { $suiteAbs = Join-Path $repoPath $SuiteScript }

  if (-not (Test-Path -LiteralPath $suiteAbs -PathType Leaf)) {
    $out = [ordered]@{
      schema="family_soak_strict_v1"
      ts_utc=UtcNowIso
      ok=$false
      exit_code=$RC_INFRA
      repo=$repoPath
      soak_run_id=$soakRunId
      error=@{ kind="infra"; type="missing_suite_script"; message="suite_script_not_found"; path=$suiteAbs }
      iterations=@()
    }
    Write-Output ($out | ConvertTo-Json -Compress -Depth 80)
    exit $RC_INFRA
  }

  $soakRunDir  = Join-Path $repoPath ("args\data\runs\" + $soakRunId)
  $evidenceDir = Join-Path $soakRunDir "evidence"
  $eventsJsonl = Join-Path $soakRunDir "events.jsonl"
  $finalJson   = Join-Path $soakRunDir "final_report.json"

  Ensure-Dir $soakRunDir
  Ensure-Dir $evidenceDir

  Append-Event $eventsJsonl $soakRunId "start" @{
    repo=$repoPath; soak_run_id=$soakRunId; n=$N;
    run_acceptance=$RunAcceptance; include_chaos=$IncludeChaos; matrix_path=$MatrixPath;
    suite_script=$suiteAbs; stop_on_first_failure=$StopOnFirstFailure
  }

  for ($i=1; $i -le $N; $i++) {
    $iterTag = ("iter_{0:D2}" -f $i)
    $stdoutPath = Join-Path $evidenceDir ($iterTag + ".suite.stdout.txt")
    $stderrPath = Join-Path $evidenceDir ($iterTag + ".suite.stderr.txt")

    if (Test-Path -LiteralPath $stdoutPath) { Remove-Item -Force $stdoutPath }
    if (Test-Path -LiteralPath $stderrPath) { Remove-Item -Force $stderrPath }

    Append-Event $eventsJsonl $soakRunId "iter_start" @{ iter=$i }

    $p = Start-Process -FilePath "powershell" -WorkingDirectory $repoPath -Wait -PassThru `
      -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath `
      -ArgumentList @(
        "-NoProfile","-ExecutionPolicy","Bypass",
        "-File",$suiteAbs,
        "-Repo",$repoPath,
        "-RunAcceptance",$RunAcceptance,
        "-IncludeChaos",$IncludeChaos,
        "-MatrixPath",$MatrixPath
      )

    $rc_raw = [int]$p.ExitCode
    $raw = Read-TextUtf8Sig $stdoutPath

    $parse_ok = $true
    $suite = $null
    try { $suite = ($raw | ConvertFrom-Json -ErrorAction Stop) } catch { $parse_ok = $false }

    $suiteRunId = ""
    $suiteExit = 2
    $suiteOk = $false

    if ($parse_ok) {
      try { $suiteRunId = [string]$suite.suite_run_id } catch { $suiteRunId = "" }
      if ([string]::IsNullOrWhiteSpace($suiteRunId)) {
        try { $suiteRunId = [string]$suite.run_id } catch { $suiteRunId = "" }
      }
      try { $suiteExit = Normalize-Exit $suite.exit_code } catch { $suiteExit = 2 }
      try { $suiteOk = [bool]$suite.ok } catch { $suiteOk = $false }
    }

    $iterInfra = $false
    $iterFail  = $false

    if (-not $parse_ok) {
      $iterInfra = $true
    } elseif ($suiteExit -eq 2 -or $rc_raw -eq 2) {
      $iterInfra = $true
    } elseif ($suiteExit -ne 0 -or (-not $suiteOk) -or $rc_raw -ne 0) {
      $iterFail = $true
    }

    if ($iterInfra) { $infraHit = $true }
    if ($iterFail)  { $failHit  = $true }

    $iters += [ordered]@{
      iter=$i
      rc_raw=$rc_raw
      parse_ok=$parse_ok
      suite_run_id=$suiteRunId
      suite_exit_code=$suiteExit
      suite_ok=$suiteOk
      stdout_path=$stdoutPath
      stderr_path=$stderrPath
    }

    Append-Event $eventsJsonl $soakRunId "iter_done" @{
      iter=$i; rc_raw=$rc_raw; parse_ok=$parse_ok; suite_run_id=$suiteRunId; suite_exit_code=$suiteExit; suite_ok=$suiteOk;
      stdout_path=$stdoutPath; stderr_path=$stderrPath
    }

    if ($StopOnFirstFailure -eq "YES" -and ($iterInfra -or $iterFail)) {
      Append-Event $eventsJsonl $soakRunId "stop" @{ iter=$i; reason=$(if ($iterInfra) { "infra" } else { "fail" }) }
      break
    }
  }

  $completed = $iters.Count
  $passed = @($iters | Where-Object { $_.parse_ok -eq $true -and $_.suite_exit_code -eq 0 -and $_.suite_ok -eq $true -and $_.rc_raw -eq 0 }).Count
  $failed = $completed - $passed

  $exitCode = $(if ($infraHit) { $RC_INFRA } elseif ($failed -gt 0) { $RC_FAIL } else { $RC_OK })
  $ok = ($exitCode -eq 0 -and $completed -eq $N)

  $final = [ordered]@{
    schema="family_soak_strict_v1"
    ts_utc=UtcNowIso
    ok=$ok
    exit_code=$exitCode
    repo=$repoPath
    soak_run_id=$soakRunId
    soak_run_dir=$soakRunDir
    evidence_dir=$evidenceDir
    events_jsonl=$eventsJsonl
    final_report_json=$finalJson
    config=@{
      n=$N
      run_acceptance=$RunAcceptance
      include_chaos=$IncludeChaos
      matrix_path=$MatrixPath
      suite_script=$suiteAbs
      stop_on_first_failure=$StopOnFirstFailure
    }
    summary=@{
      completed=$completed
      passed=$passed
      failed=$failed
      infra=$infraHit
    }
    iterations=$iters
  }

  # Contract gate on SOAK run dir (strict, with BOM scrub)
  $cg = Run-ContractGate -EvidenceDir $evidenceDir -RunDir $soakRunDir
  $final.contract_gate = $cg
  if ([int]$cg.rc -ne 0) {
    $final.ok = $false
    $final.exit_code = [int]([Math]::Max([int]$final.exit_code, [int]$cg.rc))
  }

  Write-JsonAtomic $finalJson $final
  Append-Event $eventsJsonl $soakRunId "done" @{ ok=$final.ok; exit_code=$final.exit_code; completed=$completed; passed=$passed; failed=$failed; infra=$infraHit; contract_gate_rc=$cg.rc }

  Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
  exit ([int]$final.exit_code)

} catch {
  $msg = $_.Exception.Message
  $out = [ordered]@{
    schema="family_soak_strict_v1"
    ts_utc=UtcNowIso
    ok=$false
    exit_code=$RC_INFRA
    repo=$repoPath
    soak_run_id=$soakRunId
    soak_run_dir=$soakRunDir
    evidence_dir=$evidenceDir
    events_jsonl=$eventsJsonl
    final_report_json=$finalJson
    error=@{ kind="infra"; type="unhandled_exception"; message=$msg }
    iterations=$iters
  }
  try { if ($finalJson) { Write-JsonAtomic $finalJson $out } } catch {}
  try { if ($eventsJsonl) { Append-Event $eventsJsonl $soakRunId "error" @{ message=$msg } } } catch {}
  Write-Output ($out | ConvertTo-Json -Compress -Depth 80)
  exit $RC_INFRA
}
