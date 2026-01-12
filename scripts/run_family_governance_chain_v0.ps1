param(
  [Parameter(Mandatory=$true)][string]$TargetRunId,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$SkipVaultStage = "NO"
  [Parameter(Mandatory=$false)][string]$Config = "manifests\family_chain\family_chain_config_v0.json",
  [Parameter(Mandatory=$false)][string]$ActorOverride = "",
  [Parameter(Mandatory=$false)][string]$ChannelOverride = "",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$ChaosCorruptZipBeforeVerify = "NO"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exit codes
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

# UTF-8 no BOM writers
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
    step   = "family_governance_chain_v0"
    kind   = $Kind
    data   = $Data
  }
  $line = (($ev | ConvertTo-Json -Compress -Depth 80) + "`n")
  Append-TextUtf8NoBom $EventsPath $line
}

# STRICT: external stdout must be exactly one JSON document
function Parse-OneJsonStrict([string]$Raw) {
  if ($null -eq $Raw) { return $null }
  $s = $Raw.Trim()
  if ($s -eq "") { return $null }
  try { return ($s | ConvertFrom-Json -ErrorAction Stop) } catch { return $null }
}

function Read-JsonUtf8Sig([string]$Path) {
  $raw = Get-Content -Raw -Encoding utf8 -Path $Path
  if ($null -eq $raw) { $raw = "" }
  $raw = ($raw -replace "^\uFEFF","")
  return ($raw | ConvertFrom-Json -ErrorAction Stop)
}

function Resolve-Abs([string]$Base, [string]$P) {
  if ([string]::IsNullOrWhiteSpace($P)) { return "" }
  if ([System.IO.Path]::IsPathRooted($P)) { return $P }
  return (Join-Path $Base $P)
}

# IMPORTANT: return flat string[] (never nested, never $null)
function Replace-Tokens([AllowNull()][object[]]$Argv, [hashtable]$Map) {
  $out = @()
  if ($null -ne $Argv) {
    foreach ($a in $Argv) {
      $s = [string]$a
      foreach ($k in $Map.Keys) {
        $s = $s.Replace("{"+$k+"}", [string]$Map[$k])
      }
      $out += $s
    }
  }
  return $out
}

function Quote-WinArg([string]$s) {
  if ($null -eq $s) { return '""' }
  if ($s -eq "") { return '""' }
  if ($s -notmatch '[\s"]') { return $s }

  $sb = New-Object System.Text.StringBuilder
  [void]$sb.Append('"')

  $bsCount = 0
  for ($i = 0; $i -lt $s.Length; $i++) {
    $ch = $s[$i]
    if ($ch -eq '\') { $bsCount++; continue }
    if ($ch -eq '"') {
      if ($bsCount -gt 0) { [void]$sb.Append([char]'\', ($bsCount * 2) + 1) }
      else { [void]$sb.Append([char]'\', 1) }
      [void]$sb.Append('"')
      $bsCount = 0
      continue
    }
    if ($bsCount -gt 0) { [void]$sb.Append([char]'\', $bsCount); $bsCount = 0 }
    [void]$sb.Append($ch)
  }
  if ($bsCount -gt 0) { [void]$sb.Append([char]'\', ($bsCount * 2)) }
  [void]$sb.Append('"')
  return $sb.ToString()
}

function Split-CmdArray([AllowNull()][object[]]$Cmd) {
  if ($null -eq $Cmd -or $Cmd.Count -lt 1) { return @("", @()) }
  $exe = [string]$Cmd[0]
  $argv = @()
  for ($i = 1; $i -lt $Cmd.Count; $i++) { $argv += [string]$Cmd[$i] }
  return @($exe, $argv)
}

function Invoke-External {
  param(
    [Parameter(Mandatory=$true)][string]$RepoPath,
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string[]]$Argv,
    [Parameter(Mandatory=$true)][string]$StdoutPath,
    [Parameter(Mandatory=$true)][string]$StderrPath,
    [Parameter(Mandatory=$false)][int]$TimeoutMs = 120000
  )

  if ($null -eq $Argv -or $Argv.Count -eq 0) {
    Write-TextUtf8NoBom $StdoutPath ""
    Write-TextUtf8NoBom $StderrPath "argv_empty"
    return [ordered]@{
      infra=$true; rc=2; rc_raw=2; json=$null;
      stdout_path=$StdoutPath; stderr_path=$StderrPath;
      cmd=@($Exe)
    }
  }

  # Guard: enforce Python 3.11 selector for py
  $exeLower = ([string]$Exe).ToLowerInvariant()
  if (($exeLower -eq "py" -or $exeLower -eq "py.exe") -and (-not ($Argv | Where-Object { $_ -like "-3.11*" }))) {
    Write-TextUtf8NoBom $StdoutPath ""
    Write-TextUtf8NoBom $StderrPath "py_missing_-3.11"
    return [ordered]@{
      infra=$true; rc=2; rc_raw=2; json=$null;
      stdout_path=$StdoutPath; stderr_path=$StderrPath;
      cmd=@($Exe) + @($Argv)
    }
  }

  $outText = ""
  $errText = ""
  $rc_raw = 2

  try {
    if (-not (Test-Path -LiteralPath $RepoPath -PathType Container)) { throw "missing_repo: $RepoPath" }
    if ([string]::IsNullOrWhiteSpace($Exe)) { throw "missing_exe" }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.WorkingDirectory = $RepoPath
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow = $true
    $psi.Arguments = (($Argv | ForEach-Object { Quote-WinArg $_ }) -join " ")

    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi

    $null = $p.Start()
    $outText = $p.StandardOutput.ReadToEnd()
    $errText = $p.StandardError.ReadToEnd()

    $exited = $p.WaitForExit($TimeoutMs)
    if (-not $exited) {
      try { $p.Kill() } catch {}
      $rc_raw = 2
      $errText = ($errText + "`nTIMEOUT_MS=" + $TimeoutMs)
      $outText = ""
    } else {
      $rc_raw = $p.ExitCode
    }
  } catch {
    $rc_raw = 2
    $outText = ""
    $errText = (($_ | Out-String))
  }

  try { Write-TextUtf8NoBom $StdoutPath $outText } catch {}
  try { Write-TextUtf8NoBom $StderrPath $errText } catch {}

  $rc = Normalize-Exit $rc_raw
  $obj = Parse-OneJsonStrict $outText

  $infra = $false
  if ($null -eq $obj) { $infra = $true; $rc = 2; $rc_raw = 2 }

  return [ordered]@{
    infra=$infra; rc=$rc; rc_raw=$rc_raw; json=$obj;
    stdout_path=$StdoutPath; stderr_path=$StderrPath;
    cmd=@($Exe) + @($Argv)
  }
}

function Corrupt-ZipOneByte([string]$ZipPath) {
  try {
    $b = [IO.File]::ReadAllBytes($ZipPath)
    if ($b.Length -gt 100) {
      $b[100] = ($b[100] -bxor 0xFF)
      [IO.File]::WriteAllBytes($ZipPath, $b)
      return $true
    }
  } catch {}
  return $false
}

function Pick-ProducedZip([string]$VaultRepo, [DateTime]$AfterLocalTime, [string]$PreferId) {
  $dirs = @(
    (Join-Path $VaultRepo "dist"),
    (Join-Path $VaultRepo "args\data"),
    $VaultRepo
  )

  $cands = @()
  foreach ($d in $dirs) {
    if (Test-Path -LiteralPath $d -PathType Container) {
      $cands += Get-ChildItem -LiteralPath $d -Recurse -Filter *.zip -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $AfterLocalTime }
    }
  }

  if ($cands.Count -eq 0) { return "" }

  # Prefer zips containing run_id in name
  $pref = $cands | Where-Object { $_.Name -like "*$PreferId*" } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($null -ne $pref) { return $pref.FullName }

  $latest = $cands | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($null -ne $latest) { return $latest.FullName }
  return ""
}

# ---------------- MAIN ----------------
$repoPath = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

# RepoGuard
$repoGuard = Join-Path $repoPath ".args_engine_repo"
if (-not (Test-Path -LiteralPath $repoGuard -PathType Leaf)) {
  $finalFail = [ordered]@{
    schema="family_governance_chain_v0"
    ts_utc=UtcNowIso
    ok=$false
    exit_code=$RC_INFRA
    repo=$repoPath
    run_id=""
    reason="wrong_repo"
    error=@{ kind="exception"; message="WRONG_REPO" }
    steps=@()
  }
  Write-Output ($finalFail | ConvertTo-Json -Compress -Depth 20)
  exit $RC_INFRA
}

$runId = (UtcNowId) + "_" + (RandHex 8)
$runDir = Join-Path $repoPath ("args\data\runs\" + $runId)
$evidenceDir = Join-Path $runDir "evidence"
$eventsJsonl = Join-Path $runDir "events.jsonl"
$finalJson   = Join-Path $runDir "final_report.json"

Ensure-Dir $runDir
Ensure-Dir $evidenceDir

$tsStart = UtcNowIso

$steps = @()
$exitCode = 0
$ok = $true
$reason = "OK"
$error_msg = ""

# resolved paths (for final report)
$cfgAbs = Resolve-Abs $repoPath $Config
$foundryRepo = $repoPath
$guardianRepo = ""
$governorRepo = ""
$vaultRepo = ""
$targetFinal = ""

Append-Event $eventsJsonl $runId "start" @{
  target_run_id=$TargetRunId; config=$cfgAbs
}

try {
  # ---------- CONFIG ----------
  if (-not (Test-Path -LiteralPath $cfgAbs -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_config"
    throw "missing_config: $cfgAbs"
  }

  $cfg = Read-JsonUtf8Sig $cfgAbs
  if ([string]$cfg.schema -ne "family_chain_config_v0") {
    $exitCode = 2; $ok = $false; $reason = "bad_config_schema"
    throw "bad_config_schema"
  }

  $rawCfg = Get-Content -Raw -Encoding utf8 -Path $cfgAbs
  if ($null -eq $rawCfg) { $rawCfg = "" }
  $rawCfg = ($rawCfg -replace "^\uFEFF","")
  if ($rawCfg -match "__SET__") {
    $exitCode = 2; $ok = $false; $reason = "config_has___SET__"
    throw "config_has___SET__"
  }

  $foundryRepo  = Resolve-Abs $repoPath ([string]$cfg.foundry.repo)
  $guardianRepo = Resolve-Abs $repoPath ([string]$cfg.guardian.repo)
  $governorRepo = Resolve-Abs $repoPath ([string]$cfg.governor.repo)
  $vaultRepo    = Resolve-Abs $repoPath ([string]$cfg.vault.repo)

  if (-not (Test-Path -LiteralPath $foundryRepo -PathType Container)) { throw "missing_foundry_repo: $foundryRepo" }
  if (-not (Test-Path -LiteralPath $guardianRepo -PathType Container)) { throw "missing_guardian_repo: $guardianRepo" }
  if (-not (Test-Path -LiteralPath $governorRepo -PathType Container)) { throw "missing_governor_repo: $governorRepo" }
  if (-not (Test-Path -LiteralPath $vaultRepo -PathType Container)) { throw "missing_vault_repo: $vaultRepo" }

  # ---------- TARGET ----------
  $targetFinal = Join-Path $foundryRepo ("args\data\runs\" + $TargetRunId + "\final_report.json")
  if (-not (Test-Path -LiteralPath $targetFinal -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_target_final_report"
    throw "missing_target_final_report: $targetFinal"
  }

  # actor/channel
  $actor = [string]$cfg.foundry.actor
  $channel = [string]$cfg.foundry.channel
  if (-not [string]::IsNullOrWhiteSpace($ActorOverride)) { $actor = $ActorOverride }
  if (-not [string]::IsNullOrWhiteSpace($ChannelOverride)) { $channel = $ChannelOverride }

  # Common Foundry run paths
  $foundryRunsDir = Join-Path $foundryRepo "args\data\runs"
  $foundryRunDir  = Join-Path $foundryRunsDir $TargetRunId

  # ---------- GUARDIAN ----------
  $gPolicy = Resolve-Abs $guardianRepo ([string]$cfg.guardian.policy_path)
  if (-not (Test-Path -LiteralPath $gPolicy -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_guardian_policy"
    throw "missing_guardian_policy: $gPolicy"
  }

  $gOutDir = Join-Path $evidenceDir "guardian_out"
  Ensure-Dir $gOutDir

  if (-not (Test-Path -LiteralPath $foundryRunDir -PathType Container)) {
    $exitCode = 2; $ok = $false; $reason = "missing_target_run_dir"
    throw "missing_target_run_dir: $foundryRunDir"
  }

  $gReqPath = Join-Path $evidenceDir "guardian_request.json"
  $gReq = [ordered]@{
    schema     = "guardian_request_v0"
    request_id = $runId
    ts_utc     = UtcNowIso
    actor      = $actor
    action     = "foundry.release_export"
    target     = [ordered]@{ type="path"; path=$foundryRunDir }
    reason     = "foundry pre-export governance chain"
    constraints= [ordered]@{ dryrun=$true; timeout_s=30; max_items=10 }
    context    = [ordered]@{ repo="ARGS-Engine-Foundry-v0"; env="local" }
  }
  Write-JsonAtomic $gReqPath $gReq

  $gCmdArr = @($cfg.guardian.check_cmd)
  if ($null -eq $gCmdArr -or $gCmdArr.Count -lt 2) { throw "guardian_check_cmd_empty" }
  $gSplit = Split-CmdArray $gCmdArr
  $gExe = [string]$gSplit[0]
  $gArgvTemplate = [string[]]$gSplit[1]

  $tokG = @{ request=$gReqPath; policy=$gPolicy; out_dir=$gOutDir }
  $gArgv = [string[]](Replace-Tokens $gArgvTemplate $tokG)
  if ($null -eq $gArgv -or $gArgv.Count -lt 1) { throw "guardian_args_empty" }

  $gStdout = Join-Path $evidenceDir "guardian.stdout.txt"
  $gStderr = Join-Path $evidenceDir "guardian.stderr.txt"

  $gr = Invoke-External -RepoPath $guardianRepo -Exe $gExe -Argv $gArgv -StdoutPath $gStdout -StderrPath $gStderr
  Append-Event $eventsJsonl $runId "guardian_done" @{ rc=$gr.rc; rc_raw=$gr.rc_raw; infra=$gr.infra }

  $steps += [ordered]@{
    step="guardian_check"
    rc=$gr.rc; rc_raw=$gr.rc_raw; infra=$gr.infra
    cmd=$gr.cmd
    stdout_path=$gStdout; stderr_path=$gStderr
    request_json=$gReqPath; policy_path=$gPolicy; out_dir=$gOutDir
  }

  if ([int]$gr.rc -ne 0) {
    $exitCode = [int]$gr.rc
    $ok = $false
    $reason = ($(if ($gr.rc -eq 2) { "guardian_infra" } else { "guardian_blocked" }))
    throw $reason
  }

  # ---------- GOVERNOR ----------
  $govPolicy = Resolve-Abs $governorRepo ([string]$cfg.governor.policy_path)
  if (-not (Test-Path -LiteralPath $govPolicy -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_governor_policy"
    throw "missing_governor_policy: $govPolicy"
  }

  $govOutPath = Join-Path $evidenceDir "governor_report.json"

  $govCmdArr = @($cfg.governor.check_cmd)
  if ($null -eq $govCmdArr -or $govCmdArr.Count -lt 2) { throw "governor_check_cmd_empty" }
  $govSplit = Split-CmdArray $govCmdArr
  $govExe = [string]$govSplit[0]
  $govArgvTemplate = [string[]]$govSplit[1]

  $tok2 = @{ final_report=$targetFinal; policy=$govPolicy; out_path=$govOutPath }
  $govArgv = [string[]](Replace-Tokens $govArgvTemplate $tok2)
  if ($null -eq $govArgv -or $govArgv.Count -lt 1) { throw "governor_args_empty" }

  $govStdout = Join-Path $evidenceDir "governor.stdout.txt"
  $govStderr = Join-Path $evidenceDir "governor.stderr.txt"

  $rr = Invoke-External -RepoPath $governorRepo -Exe $govExe -Argv $govArgv -StdoutPath $govStdout -StderrPath $govStderr
  Append-Event $eventsJsonl $runId "governor_done" @{ rc=$rr.rc; rc_raw=$rr.rc_raw; infra=$rr.infra }

  $steps += [ordered]@{
    step="governor_check"
    rc=$rr.rc; rc_raw=$rr.rc_raw; infra=$rr.infra
    cmd=$rr.cmd
    stdout_path=$govStdout; stderr_path=$govStderr
    final_report=$targetFinal; policy_path=$govPolicy; report_path=$govOutPath
  }

  if ([int]$rr.rc -ne 0) {
    $exitCode = [int]$rr.rc
    $ok = $false
    $reason = ($(if ($rr.rc -eq 2) { "governor_infra" } else { "governor_blocked" }))
    throw $reason
  }

  # ---------- VAULT EXPORT + VERIFY ----------
  $vaultCfg = Resolve-Abs $vaultRepo ([string]$cfg.vault.vault_config_path)
  if (-not (Test-Path -LiteralPath $vaultCfg -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_vault_config"
    throw "missing_vault_config: $vaultCfg"
  }

  $zipPath = Join-Path $evidenceDir ("audit_bundle_" + $TargetRunId + ".zip")

  # Stage Foundry run into Vault runs store (Vault CLI v0 reads from its own repo)
  $vaultRunsDir = Join-Path $vaultRepo "args\data\runs"
  $vaultRunDir  = Join-Path $vaultRunsDir $TargetRunId
  Ensure-Dir $vaultRunsDir

  if (Test-Path -LiteralPath $vaultRunDir -PathType Container) {
    Remove-Item -Recurse -Force -LiteralPath $vaultRunDir
  }
  Copy-Item -Recurse -Force -LiteralPath $foundryRunDir -Destination $vaultRunDir

  $stageNote = "STAGED_RUN`nSRC=$foundryRunDir`nDST=$vaultRunDir`nTS=$(UtcNowIso)`n"
  Write-TextUtf8NoBom (Join-Path $evidenceDir "vault_stage.txt") $stageNote

  # Vault export: v0 supports ONLY --run-id and --config
  $vExpCmdArr = @($cfg.vault.export_cmd)
  if ($null -eq $vExpCmdArr -or $vExpCmdArr.Count -lt 2) { throw "vault_export_cmd_empty" }
  $vExpSplit = Split-CmdArray $vExpCmdArr
  $vExpExe = [string]$vExpSplit[0]
  $vExpArgvTemplate = [string[]]$vExpSplit[1]

  $tok3 = @{
    run_id       = $TargetRunId
    vault_config = $vaultCfg
    run_dir      = $foundryRunDir
    zip_path     = $zipPath
  }
  $vExpArgv = [string[]](Replace-Tokens $vExpArgvTemplate $tok3)
  if ($null -eq $vExpArgv -or $vExpArgv.Count -lt 1) { throw "vault_export_args_empty" }

  $vExpStdout = Join-Path $evidenceDir "vault_export.stdout.txt"
  $vExpStderr = Join-Path $evidenceDir "vault_export.stderr.txt"

  $tExportStart = Get-Date
  $vr = Invoke-External -RepoPath $vaultRepo -Exe $vExpExe -Argv $vExpArgv -StdoutPath $vExpStdout -StderrPath $vExpStderr
  Append-Event $eventsJsonl $runId "vault_export_done" @{ rc=$vr.rc; rc_raw=$vr.rc_raw; infra=$vr.infra }

  $steps += [ordered]@{
    step="vault_export"
    rc=$vr.rc; rc_raw=$vr.rc_raw; infra=$vr.infra
    cmd=$vr.cmd
    stdout_path=$vExpStdout; stderr_path=$vExpStderr
    vault_config=$vaultCfg; staged_run_dir=$vaultRunDir; expected_zip_path=$zipPath
  }

  if ([int]$vr.rc -ne 0) {
    $exitCode = [int]$vr.rc
    $ok = $false
    $reason = ($(if ($vr.rc -eq 2) { "vault_export_infra" } else { "vault_export_failed" }))
    throw $reason
  }

  # Determine produced zip and copy into chain evidence zipPath
  $producedZip = ""
  if ($null -ne $vr.json) {
    foreach ($k in @("zip_path","zip","bundle_zip","bundle_path","archive_path","out_path","path")) {
      if ($vr.json.PSObject.Properties.Name -contains $k) {
        $v = [string]$vr.json.$k
        if (-not [string]::IsNullOrWhiteSpace($v) -and ($v.ToLowerInvariant().EndsWith(".zip"))) { $producedZip = $v; break }
      }
    }
  }
  if ([string]::IsNullOrWhiteSpace($producedZip)) {
    $producedZip = Pick-ProducedZip -VaultRepo $vaultRepo -AfterLocalTime $tExportStart.AddSeconds(-2) -PreferId $TargetRunId
  }
  if ([string]::IsNullOrWhiteSpace($producedZip)) {
    $exitCode = 2; $ok = $false; $reason = "vault_export_missing_zip"
    throw "vault_export_missing_zip"
  }
  if (-not (Test-Path -LiteralPath $producedZip -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "vault_export_zip_not_found"
    throw "vault_export_zip_not_found: $producedZip"
  }

  Copy-Item -Force -LiteralPath $producedZip -Destination $zipPath
  Append-Event $eventsJsonl $runId "vault_zip_selected" @{ produced_zip=$producedZip; copied_to=$zipPath }

  if ($ChaosCorruptZipBeforeVerify -eq "YES") {
    $did = Corrupt-ZipOneByte $zipPath
    Append-Event $eventsJsonl $runId "chaos_zip_corrupt" @{ applied=$did; zip=$zipPath }
  }

  # Vault verify (positional zip path)
  $vVerCmdArr = @($cfg.vault.verify_cmd)
  if ($null -eq $vVerCmdArr -or $vVerCmdArr.Count -lt 2) { throw "vault_verify_cmd_empty" }
  $vVerSplit = Split-CmdArray $vVerCmdArr
  $vVerExe = [string]$vVerSplit[0]
  $vVerArgvTemplate = [string[]]$vVerSplit[1]

  $tok4 = @{ zip_path=$zipPath }
  $vVerArgv = [string[]](Replace-Tokens $vVerArgvTemplate $tok4)
  if ($null -eq $vVerArgv -or $vVerArgv.Count -lt 1) { throw "vault_verify_args_empty" }

  $vVerStdout = Join-Path $evidenceDir "vault_verify.stdout.txt"
  $vVerStderr = Join-Path $evidenceDir "vault_verify.stderr.txt"

  $vv = Invoke-External -RepoPath $vaultRepo -Exe $vVerExe -Argv $vVerArgv -StdoutPath $vVerStdout -StderrPath $vVerStderr
  Append-Event $eventsJsonl $runId "vault_verify_done" @{ rc=$vv.rc; rc_raw=$vv.rc_raw; infra=$vv.infra }

  $steps += [ordered]@{
    step="vault_verify"
    rc=$vv.rc; rc_raw=$vv.rc_raw; infra=$vv.infra
    cmd=$vv.cmd
    stdout_path=$vVerStdout; stderr_path=$vVerStderr
    zip_path=$zipPath
  }

  if ([int]$vv.rc -ne 0) {
    $exitCode = [int]$vv.rc
    $ok = $false
    $reason = ($(if ($vv.rc -eq 2) { "vault_verify_infra" } else { "vault_verify_failed" }))
    throw $reason
  }

  $exitCode = 0
  $ok = $true
  $reason = "OK"

} catch {
  $error_msg = $_.Exception.Message
  try {
    $chainErr = Join-Path $evidenceDir "chain_exception.txt"
    Write-TextUtf8NoBom $chainErr (($_ | Out-String))
  } catch {}

  if ($exitCode -eq 0) { $exitCode = 2; $ok = $false }
  if ([string]::IsNullOrWhiteSpace($reason)) { $reason = "unhandled_exception" }
}

$final = [ordered]@{
  schema="family_governance_chain_v0"
  ts_utc=UtcNowIso
  ok=$ok
  exit_code=[int]$exitCode
  repo=$repoPath
  run_id=$runId
  run_dir=$runDir
  evidence_dir=$evidenceDir
  events_jsonl=$eventsJsonl
  final_report_json=$finalJson
  started_utc=$tsStart
  ended_utc=UtcNowIso

  config_used=$Config
  config_abs=$cfgAbs

  foundry_repo=$foundryRepo
  guardian_repo=$guardianRepo
  governor_repo=$governorRepo
  vault_repo=$vaultRepo

  target_run_id=$TargetRunId
  target_final_report=$targetFinal

  reason=$reason
  error=@{ kind="exception"; message=$error_msg }

  steps=$steps
}

try { Write-JsonAtomic $finalJson $final } catch {}
try { Append-Event $eventsJsonl $runId "done" @{ ok=$ok; exit_code=$exitCode; reason=$reason; target_run_id=$TargetRunId } } catch {}

Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
exit ([int]$exitCode)
