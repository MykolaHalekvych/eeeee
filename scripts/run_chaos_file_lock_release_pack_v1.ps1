param(
  [Parameter(Mandatory=$true)][string]$RunId,
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RestoreFinalReport = "YES",
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
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

function Emit-AndExit([hashtable]$Obj, [int]$Code) {
  $Obj.exit_code = $Code
  $Obj.ok = ($Code -eq 0)
  $Obj.ts_utc = UtcNowIso
  Write-Output ($Obj | ConvertTo-Json -Compress)
  exit $Code
}

function Normalize-Exit([object]$v) {
  try { $n = [int]$v } catch { return $RC_INFRA }
  if ($n -eq 0 -or $n -eq 1 -or $n -eq 2) { return $n }
  return $RC_INFRA
}

function Append-Event([string]$EventsPath, [string]$Kind, [hashtable]$Data) {
  $ev = @{
    schema = "event_v0"
    ts_utc = UtcNowIso
    run_id = $script:ChaosRunId
    step = "chaos_file_lock_release_pack_v1"
    kind = $Kind
    data = $Data
  }
  Add-Content -Encoding utf8 -Path $EventsPath -Value (($ev | ConvertTo-Json -Compress) + "`n")
}

$repoPath = (Resolve-Path $Repo).Path
$targetRunDir = Join-Path $repoPath ("args\data\runs\" + $RunId)
$targetFinal = Join-Path $targetRunDir "final_report.json"

if (-not (Test-Path -LiteralPath $targetFinal)) {
  Emit-AndExit ([ordered]@{
    schema="chaos_file_lock_release_pack_v1"; step="precheck"; repo=$repoPath; target_run_id=$RunId
    error=@{kind="fail"; type="missing_target_final_report"; message="Target final_report.json not found"; path=$targetFinal}
  }) $RC_FAIL
}

# Create CHAOS run dir for evidence
$script:ChaosRunId = (UtcNowId) + "_" + (RandHex 8)
$chaosRunDir = Join-Path $repoPath ("args\data\runs\" + $script:ChaosRunId)
$chaosEvidence = Join-Path $chaosRunDir "evidence"
$chaosEvents = Join-Path $chaosRunDir "events.jsonl"
$chaosFinal = Join-Path $chaosRunDir "final_report.json"

New-Item -ItemType Directory -Force $chaosEvidence | Out-Null
New-Item -ItemType Directory -Force $chaosRunDir | Out-Null

Append-Event $chaosEvents "start" @{ repo=$repoPath; target_run_id=$RunId }

# Read + backup target final_report
$preText = Get-Content -Raw -Encoding utf8 $targetFinal
try { $preObj = $preText | ConvertFrom-Json -ErrorAction Stop } catch {
  Emit-AndExit ([ordered]@{
    schema="chaos_file_lock_release_pack_v1"; step="precheck"; repo=$repoPath; target_run_id=$RunId; chaos_run_id=$script:ChaosRunId
    error=@{kind="fail"; type="target_final_report_invalid_json"; message=$_.Exception.Message}
  }) $RC_FAIL
}

$releaseZip = [string]$preObj.release_zip
$releaseId  = [string]$preObj.release_id

if ([string]::IsNullOrWhiteSpace($releaseZip) -or -not (Test-Path -LiteralPath $releaseZip)) {
  Emit-AndExit ([ordered]@{
    schema="chaos_file_lock_release_pack_v1"; step="precheck"; repo=$repoPath; target_run_id=$RunId; chaos_run_id=$script:ChaosRunId
    error=@{kind="fail"; type="missing_release_zip"; message="release_zip missing or not found"; release_zip=$releaseZip; release_id=$releaseId}
  }) $RC_FAIL
}

Set-Content -Encoding utf8 -Path (Join-Path $chaosEvidence "target_final_report_pre.json") -Value $preText

# SHA256 before
$shaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $releaseZip).Hash
Append-Event $chaosEvents "sha_before" @{ release_zip=$releaseZip; sha256=$shaBefore; release_id=$releaseId }

# Lock handle (deny delete by omitting FileShare.Delete)
$fs = $null
$buildRaw = ""
$buildJson = $null
$buildExit = $RC_INFRA
$shaAfter = ""

try {
  $fs = [System.IO.File]::Open(
    $releaseZip,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::ReadWrite
  )
  Append-Event $chaosEvents "locked" @{ release_zip=$releaseZip }

  $buildScript = Join-Path $repoPath "scripts\run_factory_app_build_release_v1.ps1"

  $buildRaw = (& powershell -NoProfile -ExecutionPolicy Bypass -File $buildScript `
      -RunId $RunId -RunAcceptance "YES" 2>&1 | Out-String)

  try { $buildJson = $buildRaw.Trim() | ConvertFrom-Json -ErrorAction Stop } catch { $buildJson = $null }

  if ($null -eq $buildJson) {
    $buildExit = $RC_INFRA
  } else {
    $buildExit = Normalize-Exit $buildJson.exit_code
  }

} finally {
  if ($fs -ne $null) { $fs.Dispose() }
}

# SHA256 after
$shaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $releaseZip).Hash
Append-Event $chaosEvents "sha_after" @{ release_zip=$releaseZip; sha256=$shaAfter; release_id=$releaseId }

# Save raw build output as evidence
Set-Content -Encoding utf8 -Path (Join-Path $chaosEvidence "build_release_stdout_raw.txt") -Value $buildRaw
if ($buildJson -ne $null) {
  Set-Content -Encoding utf8 -Path (Join-Path $chaosEvidence "build_release_stdout.json") -Value (($buildJson | ConvertTo-Json -Compress))
}

# Backup post final_report (may have changed)
$postText = Get-Content -Raw -Encoding utf8 $targetFinal
Set-Content -Encoding utf8 -Path (Join-Path $chaosEvidence "target_final_report_post.json") -Value $postText

# Restore target final_report if requested
$restoreOk = $false
if ($RestoreFinalReport -eq "YES") {
  try {
    Set-Content -Encoding utf8 -Path $targetFinal -Value $preText
    $restoreOk = $true
    Append-Event $chaosEvents "restored_final_report" @{ ok=$true }
  } catch {
    Append-Event $chaosEvents "restored_final_report" @{ ok=$false; message=$_.Exception.Message }
  }
}

# Pass criteria:
# 1) build_release must return INFRA(2)
# 2) release zip sha unchanged
$pass1 = ($buildExit -eq 2)
$pass2 = ($shaBefore -eq $shaAfter)

$report = [ordered]@{
  schema = "chaos_file_lock_release_pack_v1"
  repo = $repoPath
  chaos_run_id = $script:ChaosRunId
  target_run_id = $RunId
  release_id = $releaseId
  release_zip = $releaseZip
  sha256_before = $shaBefore
  sha256_after = $shaAfter
  build_release_exit_code = $buildExit
  build_release_ok = ($(if ($buildJson -ne $null) { [bool]$buildJson.ok } else { $false }))
  restore_final_report = $RestoreFinalReport
  restore_ok = $restoreOk
  pass = @{
    build_release_infra = $pass1
    zip_unchanged = $pass2
  }
}

# Write chaos final_report.json always
Set-Content -Encoding utf8 -Path $chaosFinal -Value (($report | ConvertTo-Json -Compress))
Append-Event $chaosEvents "done" @{ pass1=$pass1; pass2=$pass2; build_exit=$buildExit }

if ($pass1 -and $pass2) {
  Emit-AndExit $report $RC_OK
} else {
  Emit-AndExit $report $RC_FAIL
}
