param(
  [Parameter(Mandatory=$true)][string]$TargetRunId,
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

# IMPORTANT: never return $null; always return an array (maybe empty)
function Replace-Tokens([Parameter(Mandatory=$false)][AllowNull()][object[]]$Argv, [hashtable]$Map) {
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
  return ,$out
}

function Invoke-External {
  param(
    [Parameter(Mandatory=$true)][string]$RepoPath,
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$false)][AllowNull()][object[]]$Argv,
    [Parameter(Mandatory=$true)][string]$StdoutPath,
    [Parameter(Mandatory=$true)][string]$StderrPath
  )

  $outText = ""
  $rc_raw = 2

  # Normalize argv: $null => @()
  $argv = @()
  if ($null -ne $Argv) {
    foreach ($a in $Argv) { $argv += [string]$a }
  }

  try {
    Push-Location -Path $RepoPath
    try {
      $prevEap = $ErrorActionPreference
      $ErrorActionPreference = 'Continue'
      try {
        $outLines = & $Exe @argv 2> $StderrPath
        $rc_raw = $LASTEXITCODE
        $outText = ($outLines | Out-String)
      } finally {
        $ErrorActionPreference = $prevEap
      }
    } finally {
      Pop-Location
    }
  } catch {
    $rc_raw = 2
    $outText = ""
    try { Write-TextUtf8NoBom $StderrPath (($_ | Out-String)) } catch {}
  }

  # normalize evidence encodings (stdout+stderr) to UTF-8 no BOM
  try { Write-TextUtf8NoBom $StdoutPath $outText } catch {}
  try {
    if (Test-Path -LiteralPath $StderrPath) {
      $errText = Get-Content -Raw -Encoding utf8 -Path $StderrPath
      if ($null -eq $errText) { $errText = "" }
      $errText = ($errText -replace "^\uFEFF","")
      Write-TextUtf8NoBom $StderrPath $errText
    } else {
      Write-TextUtf8NoBom $StderrPath ""
    }
  } catch {}

  $rc = Normalize-Exit $rc_raw
  $obj = Parse-OneJsonStrict $outText

  $infra = $false
  if ($null -eq $obj) { $infra = $true; $rc = 2; $rc_raw = 2 }

  return [ordered]@{
    infra=$infra; rc=$rc; rc_raw=$rc_raw; json=$obj;
    stdout_path=$StdoutPath; stderr_path=$StderrPath;
    cmd=@($Exe) + @($argv)
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

# ---------------- MAIN ----------------
# Stable repo root (not dependent on caller cwd)
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

  # ---------- GUARDIAN ----------
  $gPolicy = Resolve-Abs $guardianRepo ([string]$cfg.guardian.policy_path)
  if (-not (Test-Path -LiteralPath $gPolicy -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_guardian_policy"
    throw "missing_guardian_policy: $gPolicy"
  }

  $gOutDir = Join-Path $evidenceDir "guardian_out"
  Ensure-Dir $gOutDir

  $gReqPath = Join-Path $evidenceDir "guardian_request.json"
  $gReq = [ordered]@{
    schema="guardian_request_v0"
    request_id=$runId
    ts_utc=UtcNowIso
    actor=$actor
    action="foundry.release_export"
    target=[ordered]@{
      type="foundry_run"
      run_id=$TargetRunId
      final_report=$targetFinal
    }
    constraints=[ordered]@{ channel=$channel }
  }
  Write-JsonAtomic $gReqPath $gReq

  $gCmd = @($cfg.guardian.check_cmd)
  if ($null -eq $gCmd -or $gCmd.Count -lt 2) { throw "guardian_check_cmd_empty" }
  $gExe = [string]$gCmd[0]
  $gArgs = @($gCmd | Select-Object -Skip 1)

  $tok = @{
    request=$gReqPath
    policy=$gPolicy
    out_dir=$gOutDir
    final_report=$targetFinal
    run_id=$TargetRunId
    channel=$channel
    actor=$actor
  }

  $gArgs2 = @(Replace-Tokens $gArgs $tok)
  if ($null -eq $gArgs2 -or $gArgs2.Count -lt 1) { throw "guardian_args_empty" }

  $gStdout = Join-Path $evidenceDir "guardian.stdout.txt"
  $gStderr = Join-Path $evidenceDir "guardian.stderr.txt"

  $gr = Invoke-External -RepoPath $guardianRepo -Exe $gExe -Argv $gArgs2 -StdoutPath $gStdout -StderrPath $gStderr
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
    $reason = "guardian_blocked"
    throw "guardian_blocked"
  }

  # ---------- GOVERNOR ----------
  $govPolicy = Resolve-Abs $governorRepo ([string]$cfg.governor.policy_path)
  if (-not (Test-Path -LiteralPath $govPolicy -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_governor_policy"
    throw "missing_governor_policy: $govPolicy"
  }

  $govOutPath = Join-Path $evidenceDir "governor_report.json"

  $govCmd = @($cfg.governor.check_cmd)
  if ($null -eq $govCmd -or $govCmd.Count -lt 2) { throw "governor_check_cmd_empty" }
  $govExe = [string]$govCmd[0]
  $govArgs = @($govCmd | Select-Object -Skip 1)

  $tok2 = @{
    final_report=$targetFinal
    policy=$govPolicy
    out_path=$govOutPath
    run_id=$TargetRunId
  }

  $govArgs2 = @(Replace-Tokens $govArgs $tok2)
  if ($null -eq $govArgs2 -or $govArgs2.Count -lt 1) { throw "governor_args_empty" }

  $govStdout = Join-Path $evidenceDir "governor.stdout.txt"
  $govStderr = Join-Path $evidenceDir "governor.stderr.txt"

  $rr = Invoke-External -RepoPath $governorRepo -Exe $govExe -Argv $govArgs2 -StdoutPath $govStdout -StderrPath $govStderr
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
    $reason = "governor_blocked"
    throw "governor_blocked"
  }

  # ---------- VAULT EXPORT + VERIFY ----------
  $vaultCfg = Resolve-Abs $vaultRepo ([string]$cfg.vault.vault_config_path)
  if (-not (Test-Path -LiteralPath $vaultCfg -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_vault_config"
    throw "missing_vault_config: $vaultCfg"
  }

  $zipPath = Join-Path $evidenceDir ("audit_bundle_" + $TargetRunId + ".zip")

  $vExpCmd = @($cfg.vault.export_cmd)
  if ($null -eq $vExpCmd -or $vExpCmd.Count -lt 2) { throw "vault_export_cmd_empty" }
  $vExpExe = [string]$vExpCmd[0]
  $vExpArgs = @($vExpCmd | Select-Object -Skip 1)

  $tok3 = @{
    run_id=$TargetRunId
    zip_path=$zipPath
    vault_config=$vaultCfg
  }

  $vExpArgs2 = @(Replace-Tokens $vExpArgs $tok3)
  if ($null -eq $vExpArgs2 -or $vExpArgs2.Count -lt 1) { throw "vault_export_args_empty" }

  $vExpStdout = Join-Path $evidenceDir "vault_export.stdout.txt"
  $vExpStderr = Join-Path $evidenceDir "vault_export.stderr.txt"

  $vr = Invoke-External -RepoPath $vaultRepo -Exe $vExpExe -Argv $vExpArgs2 -StdoutPath $vExpStdout -StderrPath $vExpStderr
  Append-Event $eventsJsonl $runId "vault_export_done" @{ rc=$vr.rc; rc_raw=$vr.rc_raw; infra=$vr.infra; zip=$zipPath }

  $steps += [ordered]@{
    step="vault_export"
    rc=$vr.rc; rc_raw=$vr.rc_raw; infra=$vr.infra
    cmd=$vr.cmd
    stdout_path=$vExpStdout; stderr_path=$vExpStderr
    vault_config=$vaultCfg; zip_path=$zipPath
  }

  if ([int]$vr.rc -ne 0) {
    $exitCode = [int]$vr.rc
    $ok = $false
    $reason = "vault_export_failed"
    throw "vault_export_failed"
  }

  if ($ChaosCorruptZipBeforeVerify -eq "YES") {
    $did = Corrupt-ZipOneByte $zipPath
    Append-Event $eventsJsonl $runId "chaos_zip_corrupt" @{ applied=$did; zip=$zipPath }
  }

  $vVerCmd = @($cfg.vault.verify_cmd)
  if ($null -eq $vVerCmd -or $vVerCmd.Count -lt 2) { throw "vault_verify_cmd_empty" }
  $vVerExe = [string]$vVerCmd[0]
  $vVerArgs = @($vVerCmd | Select-Object -Skip 1)

  $tok4 = @{ zip_path=$zipPath }

  $vVerArgs2 = @(Replace-Tokens $vVerArgs $tok4)
  if ($null -eq $vVerArgs2 -or $vVerArgs2.Count -lt 1) { throw "vault_verify_args_empty" }

  $vVerStdout = Join-Path $evidenceDir "vault_verify.stdout.txt"
  $vVerStderr = Join-Path $evidenceDir "vault_verify.stderr.txt"

  $vv = Invoke-External -RepoPath $vaultRepo -Exe $vVerExe -Argv $vVerArgs2 -StdoutPath $vVerStdout -StderrPath $vVerStderr
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
    $reason = "vault_verify_failed"
    throw "vault_verify_failed"
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
