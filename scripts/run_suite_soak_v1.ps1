param(
  [Parameter(Mandatory=$false)][int]$N = 10,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$IncludeChaos = "YES",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + "_" + ([Guid]::NewGuid().ToString("N").Substring(0,8))) }

function Ensure-Dir([string]$p) {
  if (-not (Test-Path -LiteralPath $p -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $p | Out-Null
  }
}

function Write-Text([string]$Path, [string]$Text) {
  Ensure-Dir (Split-Path -Parent $Path)
  Set-Content -Encoding utf8 -Path $Path -Value $Text
}

function Write-Json([string]$Path, [object]$Obj) {
  Ensure-Dir (Split-Path -Parent $Path)
  ($Obj | ConvertTo-Json -Compress -Depth 50) | Set-Content -Encoding utf8 -Path $Path
}

function Append-Jsonl([string]$Path, [object]$Obj) {
  Ensure-Dir (Split-Path -Parent $Path)
  ($Obj | ConvertTo-Json -Compress -Depth 50) | Add-Content -Encoding utf8 -Path $Path
}

function Normalize-Exit([object]$v) {
  try { $n = [int]$v } catch { return $RC_INFRA }
  if ($n -eq 0 -or $n -eq 1 -or $n -eq 2) { return $n }
  return $RC_INFRA
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

function Emit-AndExit([hashtable]$Obj, [int]$Code) {
  $Obj.exit_code = [int]$Code
  $Obj.ok = ([int]$Code -eq 0)
  $Obj.ts_utc = UtcNowIso
  Write-Output ($Obj | ConvertTo-Json -Compress -Depth 50)
  exit ([int]$Code)
}

# ------------- MAIN -------------
$repoPath = $Repo
$runId = ""
$runDir = ""
$evidenceDir = ""
$eventsJsonl = ""
$finalReportJson = ""

try {
  if ($N -lt 1) {
    Emit-AndExit ([ordered]@{
      schema="suite_soak_v1"
      step="preflight"
      repo=$repoPath
      error=@{kind="fail"; type="bad_n"; message="N must be >= 1"}
    }) $RC_FAIL
  }

  try { $repoPath = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { $repoPath = $Repo }

  $suiteScript = Join-Path $repoPath "scripts\run_family_test_suite_v1.ps1"
  if (-not (Test-Path -LiteralPath $suiteScript -PathType Leaf)) {
    Emit-AndExit ([ordered]@{
      schema="suite_soak_v1"
      step="preflight"
      repo=$repoPath
      error=@{kind="infra"; type="missing_suite_script"; message=("missing: {0}" -f $suiteScript)}
    }) $RC_INFRA
  }

  $runId = UtcNowId
  $runDir = Join-Path $repoPath ("args\data\runs\" + $runId)
  $evidenceDir = Join-Path $runDir "evidence"
  Ensure-Dir $evidenceDir

  $eventsJsonl = Join-Path $runDir "events.jsonl"
  $finalReportJson = Join-Path $runDir "final_report.json"

  Append-Jsonl $eventsJsonl ([ordered]@{
    schema="factory_run_event_v1"
    ts_utc=UtcNowIso
    run_id=$runId
    step="suite_soak"
    status="START"
    ok=$true
    exit_code=0
    details=@{ n_target=$N; include_chaos=$IncludeChaos; run_acceptance=$RunAcceptance; suite_script=$suiteScript }
  })

  $rows = @()
  $firstFail = $null
  $overall = 0

  for ($i=1; $i -le $N; $i++) {
    $iter = ("{0:D2}" -f $i)
    $stdoutPath = Join-Path $evidenceDir ("iter_{0}.stdout.txt" -f $iter)
    $stderrPath = Join-Path $evidenceDir ("iter_{0}.stderr.txt" -f $iter)
    $jsonPath   = Join-Path $evidenceDir ("iter_{0}.stdout.json" -f $iter)

    Append-Jsonl $eventsJsonl ([ordered]@{
      schema="factory_run_event_v1"
      ts_utc=UtcNowIso
      run_id=$runId
      step="suite_soak_iter"
      status="START"
      ok=$true
      exit_code=0
      details=@{ i=$i; stdout_path=$stdoutPath; stderr_path=$stderrPath }
    })

    $p = Start-Process -FilePath "powershell" -WorkingDirectory $repoPath -NoNewWindow -PassThru -Wait `
      -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",$suiteScript,"-RunAcceptance",$RunAcceptance,"-IncludeChaos",$IncludeChaos) `
      -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath

    $rc_raw = [int]$p.ExitCode
    $outRaw = ""
    try { $outRaw = Get-Content -Raw -LiteralPath $stdoutPath } catch { $outRaw = "" }
    $j = Parse-OneJson $outRaw

    if ($null -eq $j) {
      $overall = $RC_INFRA
      $firstFail = @{ i=$i; kind="infra"; type="suite_json_parse_failed"; rc_raw=$rc_raw; stdout_path=$stdoutPath; stderr_path=$stderrPath }
      Append-Jsonl $eventsJsonl ([ordered]@{
        schema="factory_run_event_v1"
        ts_utc=UtcNowIso
        run_id=$runId
        step="suite_soak_iter"
        status="INFRA"
        ok=$false
        exit_code=2
        details=$firstFail
      })
      break
    }

    try { Write-Json $jsonPath $j } catch {}

    $exit_code = Normalize-Exit $j.exit_code
    $suite_run_id = [string]$j.suite_run_id
    $total = [int]$j.summary.total
    $passed = [int]$j.summary.passed
    $failed = [int]$j.summary.failed
    $infra = [bool]$j.summary.infra
    $ts = [string]$j.ts_utc

    $rows += [ordered]@{
      i=$i
      exit_code=$exit_code
      ok=[bool]$j.ok
      suite_run_id=$suite_run_id
      total=$total
      passed=$passed
      failed=$failed
      infra=$infra
      ts_utc=$ts
    }

    $status = "OK"
    if ($exit_code -eq 1) { $status = "FAIL" }
    elseif ($exit_code -eq 2) { $status = "INFRA" }

    Append-Jsonl $eventsJsonl ([ordered]@{
      schema="factory_run_event_v1"
      ts_utc=UtcNowIso
      run_id=$runId
      step="suite_soak_iter"
      status=$status
      ok=($exit_code -eq 0)
      exit_code=$exit_code
      details=@{ i=$i; suite_run_id=$suite_run_id; rc_raw=$rc_raw; stdout_json=$jsonPath }
    })

    if ($exit_code -eq 2) {
      $overall = 2
      $firstFail = @{ i=$i; kind="infra"; suite_run_id=$suite_run_id; exit_code=2 }
      break
    }
    if ($exit_code -eq 1) {
      $overall = 1
      $firstFail = @{ i=$i; kind="fail"; suite_run_id=$suite_run_id; exit_code=1 }
      break
    }
  }

  if (($rows.Count -lt $N) -and ($null -eq $firstFail)) {
    $overall = 1
    $firstFail = @{ kind="incomplete"; n_ran=$rows.Count; n_target=$N; message="loop stopped early (likely interrupted)" }
  }

  $ok = ($rows.Count -eq $N -and $overall -eq 0)
  $out = [ordered]@{
    schema="suite_soak_v1"
    step="suite_soak"
    repo=$repoPath
    run_id=$runId
    run_dir=$runDir
    evidence_dir=$evidenceDir
    events_jsonl=$eventsJsonl
    final_report_json=$finalReportJson
    config=@{ n_target=$N; include_chaos=$IncludeChaos; run_acceptance=$RunAcceptance }
    summary=@{
      n_target=$N
      n_ran=$rows.Count
      ok=$ok
      failed=@($rows | Where-Object { $_.exit_code -eq 1 }).Count
      infra=@($rows | Where-Object { $_.exit_code -eq 2 }).Count
    }
    first_fail=$firstFail
    runs=$rows
  }

  try { Write-Json $finalReportJson $out } catch {}

  Append-Jsonl $eventsJsonl ([ordered]@{
    schema="factory_run_event_v1"
    ts_utc=UtcNowIso
    run_id=$runId
    step="suite_soak"
    status=($(if ($ok) {"OK"} elseif ($overall -eq 2) {"INFRA"} else {"FAIL"}))
    ok=$ok
    exit_code=$overall
    details=@{ n_ran=$rows.Count; n_target=$N }
  })

  Emit-AndExit $out $overall

} catch {
  $msg = $_.Exception.Message
  $payload = [ordered]@{
    schema="suite_soak_v1"
    step="unhandled_exception"
    repo=$repoPath
    run_id=$runId
    run_dir=$runDir
    evidence_dir=$evidenceDir
    events_jsonl=$eventsJsonl
    final_report_json=$finalReportJson
    error=@{ kind="infra"; type="exception"; message=$msg }
  }
  try {
    if ($finalReportJson -and $finalReportJson -ne "") { Write-Json $finalReportJson $payload }
    if ($eventsJsonl -and $eventsJsonl -ne "") {
      Append-Jsonl $eventsJsonl ([ordered]@{
        schema="factory_run_event_v1"
        ts_utc=UtcNowIso
        run_id=$runId
        step="suite_soak"
        status="INFRA"
        ok=$false
        exit_code=2
        details=$payload.error
      })
    }
  } catch {}
  Emit-AndExit $payload $RC_INFRA
}
