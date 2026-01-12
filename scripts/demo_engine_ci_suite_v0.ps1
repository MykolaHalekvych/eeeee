param(
  [Parameter(Mandatory=$false)][string]$Repo,
  [Parameter(Mandatory=$false)][string]$TargetRunId,
  [Parameter(Mandatory=$false)][string]$SeedKitId
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

if ([string]::IsNullOrWhiteSpace($Repo)) { $Repo = (Get-Location).Path }
if ($null -eq $TargetRunId) { $TargetRunId = "" }
if ([string]::IsNullOrWhiteSpace($SeedKitId)) { $SeedKitId = "kit_web_dashboard_v0" } # compat only

function UtcNowIso { return ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")) }
function RandHex([int]$n) {
  $rng = New-Object System.Random
  $chars = "0123456789abcdef".ToCharArray()
  $sb = New-Object System.Text.StringBuilder
  for ($i=0; $i -lt $n; $i++) { [void]$sb.Append($chars[$rng.Next(0,16)]) }
  return $sb.ToString()
}

function WriteUtf8NoBom([string]$Path, [string]$Text) {
  $enc = New-Object System.Text.UTF8Encoding $false
  $dir = Split-Path -Parent $Path
  if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Emit-And-Exit([hashtable]$Obj, [int]$Code) {
  $Obj.exit_code = $Code
  $Obj.ok = ($Code -eq 0)
  $json = ($Obj | ConvertTo-Json -Depth 90 -Compress)
  try { if ($Obj.out_dir) { WriteUtf8NoBom -Path (Join-Path $Obj.out_dir "summary.json") -Text ($json + "`n") } } catch {}
  Write-Output $json
  exit $Code
}

function Normalize-ExitCode([int]$Code) {
  if ($Code -eq 0) { return 0 }
  if ($Code -eq 1) { return 1 }
  if ($Code -eq 2) { return 2 }
  return 2
}

function TryReadJsonFile([string]$Path) {
  try {
    if (-not (Test-Path $Path)) { return $null }
    $raw = Get-Content -Raw -LiteralPath $Path
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    return ($raw | ConvertFrom-Json)
  } catch { return $null }
}

function QuoteArg([string]$s) {
  if ($null -eq $s) { return '""' }
  if ($s -match '[\s"]') {
    $e = $s -replace '"','\"'
    return '"' + $e + '"'
  }
  return $s
}

function IsValidFoundryRunId([string]$RepoRoot, [string]$RunId) {
  if ([string]::IsNullOrWhiteSpace($RunId)) { return $false }
  if ($RunId -like "BUILD_NO_LLM_SMOKE_*") { return $false }
  $d = Join-Path $RepoRoot ("args\data\runs\" + $RunId)
  if (-not (Test-Path $d)) { return $false }
  if (-not (Test-Path (Join-Path $d "final_report.json"))) { return $false }
  return $true
}

function GetExistingReleaseZipPath([object]$j, [string]$RepoRoot) {
  if ($null -eq $j) { return "" }

  $candidates = @()
  foreach ($k in @("release_zip","releaseZip","release_zip_path","releaseZipPath")) {
    if ($j.PSObject.Properties.Name -contains $k) { $candidates += [string]$j.$k }
  }
  foreach ($container in @("release","build_release","buildRelease","artifacts","result")) {
    if ($j.PSObject.Properties.Name -contains $container) {
      $o = $j.$container
      if ($null -ne $o) {
        foreach ($k in @("release_zip","releaseZip","release_zip_path","releaseZipPath")) {
          if ($o.PSObject.Properties.Name -contains $k) { $candidates += [string]$o.$k }
        }
      }
    }
  }

  foreach ($p in $candidates) {
    if ([string]::IsNullOrWhiteSpace($p)) { continue }
    $pp = $p
    try { if (-not [System.IO.Path]::IsPathRooted($pp)) { $pp = (Join-Path $RepoRoot $pp) } } catch {}
    if (Test-Path $pp) { return $pp }
  }
  return ""
}

function Run-ChildProcess {
  param(
    [Parameter(Mandatory=$true)][string]$FileName,
    [Parameter(Mandatory=$true)][string[]]$Args,
    [Parameter(Mandatory=$true)][string]$WorkingDirectory,
    [Parameter(Mandatory=$true)][string]$StdoutPath,
    [Parameter(Mandatory=$true)][string]$StderrPath
  )

  $rc = 2
  $stdout = ""
  $stderr = ""

  try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FileName
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    try { $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
    try { $psi.StandardErrorEncoding  = [System.Text.Encoding]::UTF8 } catch {}

    $qa = @()
    foreach ($a in $Args) { $qa += (QuoteArg $a) }
    $psi.Arguments = ($qa -join " ")

    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    $null = $p.Start()
    $stdout = $p.StandardOutput.ReadToEnd()
    $stderr = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    $rc = Normalize-ExitCode -Code $p.ExitCode
  }
  catch {
    $rc = 2
    $stderr = ("EXCEPTION: {0}" -f $_.Exception.Message)
  }

  WriteUtf8NoBom -Path $StdoutPath -Text $stdout
  WriteUtf8NoBom -Path $StderrPath -Text $stderr

  $jsonOk = $false
  $obj = $null
  try { $obj = ($stdout | ConvertFrom-Json); $jsonOk = $true } catch { $jsonOk = $false; $obj = $null }

  return @{ exit_code=$rc; json_ok=$jsonOk; obj=$obj }
}

function Run-ChildPs1 {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$ScriptRel,
    [Parameter(Mandatory=$true)][string]$StdoutPath,
    [Parameter(Mandatory=$true)][string]$StderrPath,
    [Parameter(Mandatory=$false)][string[]]$Args = @()
  )

  $scriptPath = Join-Path $RepoRoot $ScriptRel
  if (-not (Test-Path $scriptPath)) {
    WriteUtf8NoBom -Path $StdoutPath -Text '{"schema":"child_runner","ok":false,"exit_code":2,"error":{"kind":"SCRIPT_NOT_FOUND"}}'
    WriteUtf8NoBom -Path $StderrPath -Text ("SCRIPT_NOT_FOUND: {0}" -f $scriptPath)
    return @{ exit_code=2; json_ok=$true; obj=$null; script_path=$scriptPath }
  }

  $base = @("-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",$scriptPath)
  $all = @()
  foreach ($b in $base) { $all += $b }
  foreach ($a in $Args) { $all += $a }

  $r = Run-ChildProcess -FileName "powershell.exe" -Args $all -WorkingDirectory $RepoRoot -StdoutPath $StdoutPath -StderrPath $StderrPath
  return @{ exit_code=[int]$r.exit_code; json_ok=$r.json_ok; obj=$r.obj; script_path=$scriptPath }
}

function Extract-ChildSummary([object]$o) {
  if ($null -eq $o) { return $null }
  $s = @{ schema=$null; ok=$null; exit_code=$null; status=$null; step=$null; failed_step=$null; error_kind=$null; error_message=$null }
  foreach ($k in @("schema","ok","exit_code","status","step","failed_step")) {
    if ($o.PSObject.Properties.Name -contains $k) { $s.$k = $o.$k }
  }
  if ($o.PSObject.Properties.Name -contains "error" -and $null -ne $o.error) {
    $e = $o.error
    if ($e.PSObject.Properties.Name -contains "kind") { $s.error_kind = [string]$e.kind }
    if ($e.PSObject.Properties.Name -contains "message") { $s.error_message = [string]$e.message }
  }
  return $s
}

function Run-Step {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$OutDir,
    [Parameter(Mandatory=$true)][int]$Index,
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][string]$ScriptRel,
    [Parameter(Mandatory=$false)][string[]]$Args = @()
  )

  $stepDirName = ("{0:D2}_{1}" -f $Index, $Name)
  $stepDir = Join-Path $OutDir (Join-Path "steps" $stepDirName)
  New-Item -ItemType Directory -Force -Path $stepDir | Out-Null

  $stdoutPath = Join-Path $stepDir "stdout.json"
  $stderrPath = Join-Path $stepDir "stderr.txt"

  $started = [DateTime]::UtcNow
  $r = Run-ChildPs1 -RepoRoot $RepoRoot -ScriptRel $ScriptRel -StdoutPath $stdoutPath -StderrPath $stderrPath -Args $Args
  $durMs = [int]([TimeSpan]([DateTime]::UtcNow - $started)).TotalMilliseconds

  if (-not $r.json_ok) {
    return @{
      name=$Name; script=$ScriptRel; script_path=$r.script_path; args=$Args;
      status="INFRA"; exit_code=2; duration_ms=$durMs;
      stdout_path=$stdoutPath; stderr_path=$stderrPath; child_json_ok=$false; child_summary=$null; reason="CHILD_STDOUT_NOT_JSON"
    }
  }

  $status = "INFRA"
  if ($r.exit_code -eq 0) { $status = "PASS" }
  elseif ($r.exit_code -eq 1) { $status = "FAIL" }

  return @{
    name=$Name; script=$ScriptRel; script_path=$r.script_path; args=$Args;
    status=$status; exit_code=[int]$r.exit_code; duration_ms=$durMs;
    stdout_path=$stdoutPath; stderr_path=$stderrPath; child_json_ok=$true; child_summary=(Extract-ChildSummary -o $r.obj); reason=""
  }
}

function IsEligibleFactoryPass([object]$j) {
  if ($null -eq $j) { return $false }
  if (-not ($j.PSObject.Properties.Name -contains "schema")) { return $false }
  if ([string]$j.schema -ne "factory_final_report_v1") { return $false }
  if (-not ($j.PSObject.Properties.Name -contains "ok")) { return $false }
  if (-not [bool]$j.ok) { return $false }
  if (-not ($j.PSObject.Properties.Name -contains "exit_code")) { return $false }
  if ([int]$j.exit_code -ne 0) { return $false }
  return $true
}

function Governor-CheckRc {
  param(
    [Parameter(Mandatory=$true)][string]$FinalReportPath,
    [Parameter(Mandatory=$true)][string]$SelectorDir
  )

  $govRepo = "C:\Users\mukol\ARGS-Release-Governor-v0"
  $policy  = Join-Path $govRepo "manifests\release_policy_v0.json"

  $outStd = Join-Path $SelectorDir "governor_check.stdout.json"
  $outErr = Join-Path $SelectorDir "governor_check.stderr.txt"

  if (-not (Test-Path $govRepo)) {
    New-Item -ItemType Directory -Force -Path $SelectorDir | Out-Null
    WriteUtf8NoBom -Path $outStd -Text '{"schema":"governor_check","ok":false,"exit_code":2,"error":{"kind":"GOV_REPO_NOT_FOUND"}}'
    WriteUtf8NoBom -Path $outErr -Text ("GOV_REPO_NOT_FOUND: {0}" -f $govRepo)
    return 2
  }
  if (-not (Test-Path $policy)) {
    New-Item -ItemType Directory -Force -Path $SelectorDir | Out-Null
    WriteUtf8NoBom -Path $outStd -Text '{"schema":"governor_check","ok":false,"exit_code":2,"error":{"kind":"POLICY_NOT_FOUND"}}'
    WriteUtf8NoBom -Path $outErr -Text ("POLICY_NOT_FOUND: {0}" -f $policy)
    return 2
  }

  New-Item -ItemType Directory -Force -Path $SelectorDir | Out-Null

  $args = @("-3.11","-m","args.governor.release_governor_v0","check","--final-report",$FinalReportPath,"--policy",$policy,"--json")
  $r = Run-ChildProcess -FileName "py" -Args $args -WorkingDirectory $govRepo -StdoutPath $outStd -StderrPath $outErr

  if (-not $r.json_ok) { return 2 }
  return [int]$r.exit_code
}

function Discover-EligibleTargetRunId {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$SelectorBaseDir
  )

  $runsRoot = Join-Path $RepoRoot "args\data\runs"
  if (-not (Test-Path $runsRoot)) { return @{ run_id=""; mode="no_runs_root"; checked=0; gov_denied=0; gov_infra=0 } }

  $checked = 0
  $govDenied = 0
  $govInfra = 0

  $dirs = Get-ChildItem -Path $runsRoot -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
  foreach ($d in $dirs) {
    $checked += 1
    $fr = Join-Path $d.FullName "final_report.json"
    if (-not (Test-Path $fr)) { continue }

    $j = TryReadJsonFile -Path $fr
    if ($null -eq $j) { continue }

    if (-not (IsEligibleFactoryPass -j $j)) { continue }

    $rid = ""
    if ($j.PSObject.Properties.Name -contains "run_id") { $rid = [string]$j.run_id }
    if ([string]::IsNullOrWhiteSpace($rid)) { $rid = [string]$d.Name }

    if (-not (IsValidFoundryRunId -RepoRoot $RepoRoot -RunId $rid)) { continue }

    $rz = GetExistingReleaseZipPath -j $j -RepoRoot $RepoRoot
    if ([string]::IsNullOrWhiteSpace($rz)) { continue }

    $selDir = Join-Path $SelectorBaseDir ("candidate_" + $rid)
    $gRc = Governor-CheckRc -FinalReportPath $fr -SelectorDir $selDir

    if ($gRc -eq 0) {
      return @{ run_id=$rid; mode="eligible_factory_pass_releasezip_gov_pass"; checked=$checked; gov_denied=$govDenied; gov_infra=$govInfra }
    }
    elseif ($gRc -eq 2) { $govInfra += 1; continue }
    else { $govDenied += 1; continue }
  }

  $mode = "no_eligible_run"
  if ($govInfra -gt 0) { $mode = "no_eligible_run_governor_infra" }
  elseif ($govDenied -gt 0) { $mode = "no_eligible_run_governor_denied" }

  return @{ run_id=""; mode=$mode; checked=$checked; gov_denied=$govDenied; gov_infra=$govInfra }
}

$repoRoot = ""
$runId = ""
$outDir = ""

try {
  $repoRoot = (Resolve-Path $Repo).Path
  Set-Location $repoRoot
  if (-not (Test-Path (Join-Path $repoRoot ".args_engine_repo"))) { throw "WRONG_REPO" }

  $runId = ("ENGINE_CI_{0}_{1}" -f (UtcNowId), (RandHex 8))
  $outDir = Join-Path $repoRoot (Join-Path "args\data\smoke\engine_ci_suite_v0" $runId)
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null

  $selectorDir = Join-Path $outDir "selector"
  New-Item -ItemType Directory -Force -Path $selectorDir | Out-Null

  $steps = @()
  $worst = 0

  $steps += Run-Step -RepoRoot $repoRoot -OutDir $outDir -Index 1 -Name "kit_registry_smoke_v0" -ScriptRel "scripts\demo_kit_registry_smoke_v0.ps1" -Args @()
  if ($steps[-1].exit_code -gt $worst) { $worst = $steps[-1].exit_code }

  $steps += Run-Step -RepoRoot $repoRoot -OutDir $outDir -Index 2 -Name "template_pack_matrix_v0" -ScriptRel "scripts\demo_template_pack_matrix_v0.ps1" -Args @()
  if ($steps[-1].exit_code -gt $worst) { $worst = $steps[-1].exit_code }

  $resolution = @{ mode="provided_or_discovered_runs_strict_gov_pass"; ok=$true; exit_code=0; target_run_id=$TargetRunId; selector=$null }
  $resolved = $TargetRunId

  if (-not [string]::IsNullOrWhiteSpace($resolved) -and -not (IsValidFoundryRunId -RepoRoot $repoRoot -RunId $resolved)) {
    $resolution.mode="invalid_target_run_id"
    $resolution.ok=$false
    $resolution.exit_code=2
    $resolution.target_run_id=$resolved
    $resolved=""
  }

  if ([string]::IsNullOrWhiteSpace($resolved)) {
    $sel = Discover-EligibleTargetRunId -RepoRoot $repoRoot -SelectorBaseDir $selectorDir
    $resolution.mode = [string]$sel.mode
    $resolution.selector = $sel
    $resolved = [string]$sel.run_id
    $resolution.target_run_id = $resolved

    if ([string]::IsNullOrWhiteSpace($resolved)) {
      if ($sel.mode -like "*infra*") { $resolution.exit_code = 2 } else { $resolution.exit_code = 1 }
      $resolution.ok = $false
    }
  }

  WriteUtf8NoBom -Path (Join-Path $outDir "target_run_id.txt") -Text ($resolved + "`n")
  WriteUtf8NoBom -Path (Join-Path $outDir "resolution.json") -Text ((($resolution | ConvertTo-Json -Depth 60 -Compress)) + "`n")

  if ([string]::IsNullOrWhiteSpace($resolved)) {
    $rc = [int]$resolution.exit_code
    $steps += @{ name="family_chain_smoke_v0"; status="INFRA"; exit_code=$rc; reason="MISSING_ELIGIBLE_TARGET_RUN_ID"; script="scripts\demo_family_chain_smoke_v0.ps1"; script_path=""; args=@(); duration_ms=0; stdout_path=""; stderr_path=""; child_json_ok=$false; child_summary=$null }
    $steps += @{ name="family_chain_negative_no_stage_v0"; status="INFRA"; exit_code=$rc; reason="MISSING_ELIGIBLE_TARGET_RUN_ID"; script="scripts\demo_family_chain_negative_no_stage_v0.ps1"; script_path=""; args=@(); duration_ms=0; stdout_path=""; stderr_path=""; child_json_ok=$false; child_summary=$null }
    $steps += @{ name="family_chain_negative_corrupt_zip_v0"; status="INFRA"; exit_code=$rc; reason="MISSING_ELIGIBLE_TARGET_RUN_ID"; script="scripts\demo_family_chain_negative_corrupt_zip_v0.ps1"; script_path=""; args=@(); duration_ms=0; stdout_path=""; stderr_path=""; child_json_ok=$false; child_summary=$null }
    $steps += @{ name="family_chain_zip_discovery_v0"; status="INFRA"; exit_code=$rc; reason="MISSING_ELIGIBLE_TARGET_RUN_ID"; script="scripts\demo_family_chain_zip_discovery_v0.ps1"; script_path=""; args=@(); duration_ms=0; stdout_path=""; stderr_path=""; child_json_ok=$false; child_summary=$null }
    if ($rc -gt $worst) { $worst = $rc }
  }
  else {
    $familyArgs = @("-TargetRunId", $resolved)

    $steps += Run-Step -RepoRoot $repoRoot -OutDir $outDir -Index 3 -Name "family_chain_smoke_v0" -ScriptRel "scripts\demo_family_chain_smoke_v0.ps1" -Args $familyArgs
    if ($steps[-1].exit_code -gt $worst) { $worst = $steps[-1].exit_code }

    $steps += Run-Step -RepoRoot $repoRoot -OutDir $outDir -Index 4 -Name "family_chain_negative_no_stage_v0" -ScriptRel "scripts\demo_family_chain_negative_no_stage_v0.ps1" -Args $familyArgs
    if ($steps[-1].exit_code -gt $worst) { $worst = $steps[-1].exit_code }

    $steps += Run-Step -RepoRoot $repoRoot -OutDir $outDir -Index 5 -Name "family_chain_negative_corrupt_zip_v0" -ScriptRel "scripts\demo_family_chain_negative_corrupt_zip_v0.ps1" -Args $familyArgs
    if ($steps[-1].exit_code -gt $worst) { $worst = $steps[-1].exit_code }

    $steps += Run-Step -RepoRoot $repoRoot -OutDir $outDir -Index 6 -Name "family_chain_zip_discovery_v0" -ScriptRel "scripts\demo_family_chain_zip_discovery_v0.ps1" -Args $familyArgs
    if ($steps[-1].exit_code -gt $worst) { $worst = $steps[-1].exit_code }
  }

  $finalStatus = "INFRA"
  if ($worst -eq 0) { $finalStatus = "PASS" }
  elseif ($worst -eq 1) { $finalStatus = "FAIL" }

  Emit-And-Exit -Obj @{
    schema="demo_engine_ci_suite_v0";
    version="0.7";
    ts_utc=(UtcNowIso);
    repo=$repoRoot;
    run_id=$runId;
    out_dir=$outDir;
    status=$finalStatus;
    target_run_id=$resolved;
    resolution=$resolution;
    steps=$steps
  } -Code $worst
}
catch {
  try { if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null } } catch {}
  Emit-And-Exit -Obj @{
    schema="demo_engine_ci_suite_v0";
    version="0.7";
    ts_utc=(UtcNowIso);
    repo=(if ($repoRoot) { $repoRoot } else { $Repo });
    run_id=$runId;
    out_dir=$outDir;
    status="INFRA";
    error=@{ kind="exception"; message=$_.Exception.Message };
    steps=@()
  } -Code $RC_INFRA
}