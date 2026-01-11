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

function Resolve-Abs([string]$Base, [string]$P) {
  if ([string]::IsNullOrWhiteSpace($P)) { return "" }
  if ([System.IO.Path]::IsPathRooted($P)) { return $P }
  return (Join-Path $Base $P)
}

function Replace-Tokens([string[]]$Args, [hashtable]$Map) {
  $out = @()
  foreach ($a in $Args) {
    $s = [string]$a
    foreach ($k in $Map.Keys) {
      $s = $s.Replace("{"+$k+"}", [string]$Map[$k])
    }
    $out += $s
  }
  return $out
}

function Invoke-External {
  param(
    [Parameter(Mandatory=$true)][string]$RepoPath,
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string[]]$Args,
    [Parameter(Mandatory=$true)][string]$StdoutPath,
    [Parameter(Mandatory=$true)][string]$StderrPath
  )

  $outText = ""
  $rc_raw = 2

  try {
    Push-Location -Path $RepoPath
    try {
      $outLines = & $Exe @Args 2> $StderrPath
      $rc_raw = $LASTEXITCODE
      $outText = ($outLines | Out-String)
    } finally {
      Pop-Location
    }
  } catch {
    $rc_raw = 2
    $outText = ""
    try { Write-TextUtf8NoBom $StderrPath (($_ | Out-String)) } catch {}
  }

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
  $obj = Parse-OneJson $outText

  $infra = $false
  if ($null -eq $obj) { $infra = $true; $rc = 2; $rc_raw = 2 }

  return [ordered]@{
    infra=$infra; rc=$rc; rc_raw=$rc_raw; json=$obj;
    stdout_path=$StdoutPath; stderr_path=$StderrPath
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
$repoPath = (Resolve-Path .).Path

$runId = (UtcNowId) + "_" + (RandHex 8)
$runDir = Join-Path $repoPath ("args\data\runs\" + $runId)
$evidence = Join-Path $runDir "evidence"
$events = Join-Path $runDir "events.jsonl"
$finalPath = Join-Path $runDir "final_report.json"

$cases = @()
$exitCode = 0
$ok = $true
$reason = ""

try {
  # Config load
  $cfgAbs = Resolve-Abs $repoPath $Config
  if (-not (Test-Path -LiteralPath $cfgAbs -PathType Leaf)) { throw "missing_config: $cfgAbs" }

  $cfg = Read-JsonUtf8Sig $cfgAbs
  if ([string]$cfg.schema -ne "family_chain_config_v0") { throw "bad_config_schema" }

  $rawCfg = Get-Content -Raw -Encoding utf8 -Path $cfgAbs
  $rawCfg = ($rawCfg -replace "^\uFEFF","")
  if ($rawCfg -match "__SET__") { throw "config_has___SET__" }

  # Resolve repos
  $foundryRepo  = Resolve-Abs $repoPath ([string]$cfg.foundry.repo)
  $guardianRepo = [string]$cfg.guardian.repo
  $governorRepo = [string]$cfg.governor.repo
  $vaultRepo    = [string]$cfg.vault.repo

  if (-not (Test-Path -LiteralPath $guardianRepo -PathType Container)) { throw "missing_guardian_repo: $guardianRepo" }
  if (-not (Test-Path -LiteralPath $governorRepo -PathType Container)) { throw "missing_governor_repo: $governorRepo" }
  if (-not (Test-Path -LiteralPath $vaultRepo -PathType Container)) { throw "missing_vault_repo: $vaultRepo" }

  # Chain run dir
  Ensure-Dir $runDir
  Ensure-Dir $evidence

  Append-Event $events $runId "start" @{
    target_run_id=$TargetRunId; config=$cfgAbs; foundry_repo=$foundryRepo;
    guardian_repo=$guardianRepo; governor_repo=$governorRepo; vault_repo=$vaultRepo
  }

  # Target final_report
  $targetFinal = Join-Path $foundryRepo ("args\data\runs\" + $TargetRunId + "\final_report.json")
  if (-not (Test-Path -LiteralPath $targetFinal -PathType Leaf)) {
    $exitCode = 2; $ok = $false; $reason = "missing_target_final_report"
    throw "missing_target_final_report: $targetFinal"
  }

  # Actor/channel
  $actor = [string]$cfg.foundry.actor
  $channel = [string]$cfg.foundry.channel
  if (-not [string]::IsNullOrWhiteSpace($ActorOverride)) { $actor = $ActorOverride }
  if (-not [string]::IsNullOrWhiteSpace($ChannelOverride)) { $channel = $ChannelOverride }

  # ---------- GUARDIAN ----------
  $gPolicy = Resolve-Abs $guardianRepo ([string]$cfg.guardian.policy_path)
  if (-not (Test-Path -LiteralPath $gPolicy -PathType Leaf)) { throw "missing_guardian_policy: $gPolicy" }

  $gOut = Join-Path $evidence "guardian_out"
  Ensure-Dir $gOut

  $gReqPath = Join-Path $evidence "guardian_request.json"
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
  $gExe = [string]$gCmd[0]
  $gArgs = @($gCmd | Select-Object -Skip 1)

  $map = @{
    request=$gReqPath
    policy=$gPolicy
    out_dir=$gOut
    final_report=$targetFinal
    run_id=$TargetRunId
    channel=$channel
    actor=$actor
  }
  $gArgs2 = Replace-Tokens $gArgs $map

  $gStdout = Join-Path $evidence "guardian.stdout.txt"
  $gStderr = Join-Path $evidence "guardian.stderr.txt"

  $gr = Invoke-External -RepoPath $guardianRepo -Exe $gExe -Args $gArgs2 -StdoutPath $gStdout -StderrPath $gStderr
  Append-Event $events $runId "guardian_done" @{ rc=$gr.rc; infra=$gr.infra; rc_raw=$gr.rc_raw }

  $cases += [ordered]@{ step="guardian_check"; rc=$gr.rc; infra=$gr.infra; stdout=$gStdout; stderr=$gStderr }
  if ($gr.rc -ne 0) {
    $exitCode = $gr.rc
    $ok = $false
    $reason = "guardian_blocked"
    throw "guardian_blocked"
  }

  # ---------- GOVERNOR ----------
  $govPolicy = Resolve-Abs $governorRepo ([string]$cfg.governor.policy_path)
  if (-not (Test-Path -LiteralPath $govPolicy -PathType Leaf)) { throw "missing_governor_policy: $govPolicy" }

  $govOutPath = Join-Path $evidence "governor_report.json"

  $govCmd = @($cfg.governor.check_cmd)
  $govExe = [string]$govCmd[0]
  $govArgs = @($govCmd | Select-Object -Skip 1)

  $map2 = @{
    final_report=$targetFinal
    policy=$govPolicy
    out_path=$govOutPath
    run_id=$TargetRunId
  }
  $govArgs2 = Replace-Tokens $govArgs $map2

  $govStdout = Join-Path $evidence "governor.stdout.txt"
  $govStderr = Join-Path $evidence "governor.stderr.txt"

  $rr = Invoke-External -RepoPath $governorRepo -Exe $govExe -Args $govArgs2 -StdoutPath $govStdout -StderrPath $govStderr
  Append-Event $events $runId "governor_done" @{ rc=$rr.rc; infra=$rr.infra; rc_raw=$rr.rc_raw }

  $cases += [ordered]@{ step="governor_check"; rc=$rr.rc; infra=$rr.infra; stdout=$govStdout; stderr=$govStderr; report=$govOutPath }
  if ($rr.rc -ne 0) {
    $exitCode = $rr.rc
    $ok = $false
    $reason = "governor_blocked"
    throw "governor_blocked"
  }

  # ---------- VAULT EXPORT + VERIFY ----------
  $vaultCfg = Resolve-Abs $vaultRepo ([string]$cfg.vault.vault_config_path)
  if (-not (Test-Path -LiteralPath $vaultCfg -PathType Leaf)) { throw "missing_vault_config: $vaultCfg" }

  $zipPath = Join-Path $evidence ("audit_bundle_" + $TargetRunId + ".zip")

  $vExpCmd = @($cfg.vault.export_cmd)
  $vExpExe = [string]$vExpCmd[0]
  $vExpArgs = @($vExpCmd | Select-Object -Skip 1)

  $map3 = @{
    run_id=$TargetRunId
    zip_path=$zipPath
    vault_config=$vaultCfg
  }
  $vExpArgs2 = Replace-Tokens $vExpArgs $map3

  $vExpStdout = Join-Path $evidence "vault_export.stdout.txt"
  $vExpStderr = Join-Path $evidence "vault_export.stderr.txt"

  $vr = Invoke-External -RepoPath $vaultRepo -Exe $vExpExe -Args $vExpArgs2 -StdoutPath $vExpStdout -StderrPath $vExpStderr
  Append-Event $events $runId "vault_export_done" @{ rc=$vr.rc; infra=$vr.infra; rc_raw=$vr.rc_raw; zip=$zipPath }

  $cases += [ordered]@{ step="vault_export"; rc=$vr.rc; infra=$vr.infra; stdout=$vExpStdout; stderr=$vExpStderr; zip=$zipPath }
  if ($vr.rc -ne 0) {
    $exitCode = $vr.rc
    $ok = $false
    $reason = "vault_export_failed"
    throw "vault_export_failed"
  }

  if ($ChaosCorruptZipBeforeVerify -eq "YES") {
    $did = Corrupt-ZipOneByte $zipPath
    Append-Event $events $runId "chaos_zip_corrupt" @{ applied=$did; zip=$zipPath }
  }

  $vVerCmd = @($cfg.vault.verify_cmd)
  $vVerExe = [string]$vVerCmd[0]
  $vVerArgs = @($vVerCmd | Select-Object -Skip 1)

  $map4 = @{ zip_path=$zipPath }
  $vVerArgs2 = Replace-Tokens $vVerArgs $map4

  $vVerStdout = Join-Path $evidence "vault_verify.stdout.txt"
  $vVerStderr = Join-Path $evidence "vault_verify.stderr.txt"

  $vv = Invoke-External -RepoPath $vaultRepo -Exe $vVerExe -Args $vVerArgs2 -StdoutPath $vVerStdout -StderrPath $vVerStderr
  Append-Event $events $runId "vault_verify_done" @{ rc=$vv.rc; infra=$vv.infra; rc_raw=$vv.rc_raw }

  $cases += [ordered]@{ step="vault_verify"; rc=$vv.rc; infra=$vv.infra; stdout=$vVerStdout; stderr=$vVerStderr }
  if ($vv.rc -ne 0) {
    $exitCode = $vv.rc
    $ok = $false
    $reason = "vault_verify_failed"
    throw "vault_verify_failed"
  }

  $exitCode = 0
  $ok = $true
  $reason = "OK"

} catch {
  if ($exitCode -eq 0) { $exitCode = 2; $ok = $false; $reason = "unhandled_exception" }
}

$final = [ordered]@{
  schema="family_governance_chain_v0"
  ts_utc=UtcNowIso
  ok=$ok
  exit_code=[int]$exitCode
  repo=$repoPath
  run_id=$runId
  run_dir=$runDir
  evidence_dir=$evidence
  events_jsonl=$events
  final_report_json=$finalPath
  target_run_id=$TargetRunId
  target_final_report=(Join-Path $repoPath ("args\data\runs\" + $TargetRunId + "\final_report.json"))
  reason=$reason
  steps=$cases
  config_used=$Config
}

try { Write-JsonAtomic $finalPath $final } catch {}

try { Append-Event $events $runId "done" @{ ok=$ok; exit_code=$exitCode; reason=$reason; target_run_id=$TargetRunId } } catch {}

Write-Output ($final | ConvertTo-Json -Compress -Depth 80)
exit ([int]$exitCode)
