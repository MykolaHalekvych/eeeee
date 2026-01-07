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

function Emit-AndExit([hashtable]$Obj, [int]$Code) {
  $Obj.exit_code = $Code
  $Obj.ok = ($Code -eq 0)
  $Obj.ts_utc = UtcNowIso
  Write-Output ($Obj | ConvertTo-Json -Compress -Depth 30)
  exit $Code
}

function Append-Event([string]$EventsPath, [string]$RunId, [string]$Kind, [hashtable]$Data) {
  $ev = [ordered]@{
    schema = "event_v0"
    ts_utc = UtcNowIso
    run_id = $RunId
    step = "chaos_file_lock_release_pack_v1"
    kind = $Kind
    data = $Data
  }
  Ensure-Dir (Split-Path -Parent $EventsPath)
  Add-Content -Encoding utf8 -Path $EventsPath -Value ((($ev | ConvertTo-Json -Compress -Depth 30) + "`n"))
}

function Write-Text([string]$Path, [string]$Text) {
  Ensure-Dir (Split-Path -Parent $Path)
  Set-Content -Encoding utf8 -Path $Path -Value $Text
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

# ---------------- MAIN (always produce chaos run evidence) ----------------

$repoPath = ""
$chaosRunId = (UtcNowId) + "_" + (RandHex 8)
$chaosRunDir = ""
$chaosEvidence = ""
$chaosEvents = ""
$chaosFinal = ""

try {
  try { $repoPath = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { $repoPath = $Repo }

  # Create chaos run dir early (standard: always have evidence)
  $chaosRunDir = Join-Path $repoPath ("args\data\runs\" + $chaosRunId)
  $chaosEvidence = Join-Path $chaosRunDir "evidence"
  $chaosEvents = Join-Path $chaosRunDir "events.jsonl"
  $chaosFinal = Join-Path $chaosRunDir "final_report.json"

  Ensure-Dir $chaosEvidence
  Ensure-Dir $chaosRunDir

  Append-Event $chaosEvents $chaosRunId "start" @{ repo=$repoPath; target_run_id=$RunId; restore_final_report=$RestoreFinalReport }

  $targetRunDir = Join-Path $repoPath ("args\data\runs\" + $RunId)
  $targetFinal = Join-Path $targetRunDir "final_report.json"

  if (-not (Test-Path -LiteralPath $targetFinal -PathType Leaf)) {
    $report = [ordered]@{
      schema = "chaos_file_lock_release_pack_v1"
      repo = $repoPath
      chaos_run_id = $chaosRunId
      target_run_id = $RunId
      pass = @{ build_release_infra=$false; zip_unchanged=$false }
      error = @{ kind="fail"; type="missing_target_final_report"; message="Target final_report.json not found"; path=$targetFinal }
    }
    Write-Text $chaosFinal (($report | ConvertTo-Json -Compress -Depth 30))
    Append-Event $chaosEvents $chaosRunId "precheck_fail" @{ path=$targetFinal }
    Emit-AndExit $report $RC_FAIL
  }

  $preText = Get-Content -Raw -Encoding utf8 $targetFinal
  Write-Text (Join-Path $chaosEvidence "target_final_report_pre.json") $preText

  $preObj = $null
  try { $preObj = $preText | ConvertFrom-Json -ErrorAction Stop } catch {
    $report = [ordered]@{
      schema = "chaos_file_lock_release_pack_v1"
      repo = $repoPath
      chaos_run_id = $chaosRunId
      target_run_id = $RunId
      pass = @{ build_release_infra=$false; zip_unchanged=$false }
      error = @{ kind="fail"; type="target_final_report_invalid_json"; message=$_.Exception.Message }
    }
    Write-Text $chaosFinal (($report | ConvertTo-Json -Compress -Depth 30))
    Append-Event $chaosEvents $chaosRunId "precheck_fail" @{ reason="invalid_json" }
    Emit-AndExit $report $RC_FAIL
  }

  $releaseZip = [string]$preObj.release_zip
  $releaseId  = [string]$preObj.release_id

  if ([string]::IsNullOrWhiteSpace($releaseZip) -or -not (Test-Path -LiteralPath $releaseZip -PathType Leaf)) {
    $report = [ordered]@{
      schema = "chaos_file_lock_release_pack_v1"
      repo = $repoPath
      chaos_run_id = $chaosRunId
      target_run_id = $RunId
      release_id = $releaseId
      release_zip = $releaseZip
      pass = @{ build_release_infra=$false; zip_unchanged=$false }
      error = @{ kind="fail"; type="missing_release_zip"; message="release_zip missing or not found"; release_zip=$releaseZip; release_id=$releaseId }
    }
    Write-Text $chaosFinal (($report | ConvertTo-Json -Compress -Depth 30))
    Append-Event $chaosEvents $chaosRunId "precheck_fail" @{ reason="missing_release_zip"; release_zip=$releaseZip }
    Emit-AndExit $report $RC_FAIL
  }

  $shaBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath $releaseZip).Hash
  Append-Event $chaosEvents $chaosRunId "sha_before" @{ release_zip=$releaseZip; sha256=$shaBefore; release_id=$releaseId }

  # Lock + force repack into SAME release_id
  $fs = $null
  $buildRaw = ""
  $buildJson = $null
  $buildExit = $RC_INFRA

  try {
    $fs = [System.IO.File]::Open(
      $releaseZip,
      [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::Read,
      [System.IO.FileShare]::Read   # strict: deny rename/replace/delete
    )
    Append-Event $chaosEvents $chaosRunId "locked" @{ release_zip=$releaseZip; share="Read" }

    $buildScript = Join-Path $repoPath "scripts\run_factory_app_build_release_v1.ps1"

    $prevForce = $env:FOUNDRY_FORCE_REPACK
    $prevRid   = $env:FOUNDRY_RELEASE_ID_OVERRIDE
    $env:FOUNDRY_FORCE_REPACK = "1"
    $env:FOUNDRY_RELEASE_ID_OVERRIDE = $releaseId

    try {
      $buildRaw = (& powershell -NoProfile -ExecutionPolicy Bypass -File $buildScript `
        -RunId $RunId -RunAcceptance "YES" 2>&1 | Out-String)
    }
    finally {
      if ($null -eq $prevForce) { Remove-Item Env:FOUNDRY_FORCE_REPACK -ErrorAction SilentlyContinue }
      else { $env:FOUNDRY_FORCE_REPACK = $prevForce }

      if ($null -eq $prevRid) { Remove-Item Env:FOUNDRY_RELEASE_ID_OVERRIDE -ErrorAction SilentlyContinue }
      else { $env:FOUNDRY_RELEASE_ID_OVERRIDE = $prevRid }
    }

    $buildJson = Parse-OneJson $buildRaw
    if ($null -eq $buildJson) {
      $buildExit = $RC_INFRA
    } else {
      $buildExit = Normalize-Exit $buildJson.exit_code
    }
  }
  finally {
    if ($fs -ne $null) { $fs.Dispose() }
  }

  $shaAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath $releaseZip).Hash
  Append-Event $chaosEvents $chaosRunId "sha_after" @{ release_zip=$releaseZip; sha256=$shaAfter; release_id=$releaseId }

  # Evidence
  Write-Text (Join-Path $chaosEvidence "build_release_stdout_raw.txt") $buildRaw
  if ($buildJson -ne $null) {
    Write-Text (Join-Path $chaosEvidence "build_release_stdout.json") (($buildJson | ConvertTo-Json -Compress -Depth 30))
  }

  $postText = Get-Content -Raw -Encoding utf8 $targetFinal
  Write-Text (Join-Path $chaosEvidence "target_final_report_post.json") $postText

  $restoreOk = $false
  if ($RestoreFinalReport -eq "YES") {
    try {
      Set-Content -Encoding utf8 -Path $targetFinal -Value $preText
      $restoreOk = $true
      Append-Event $chaosEvents $chaosRunId "restored_final_report" @{ ok=$true }
    } catch {
      Append-Event $chaosEvents $chaosRunId "restored_final_report" @{ ok=$false; message=$_.Exception.Message }
    }
  }

  $pass1 = ($buildExit -eq 2)
  $pass2 = ($shaBefore -eq $shaAfter)

  $report = [ordered]@{
    schema = "chaos_file_lock_release_pack_v1"
    repo = $repoPath
    chaos_run_id = $chaosRunId
    target_run_id = $RunId
    release_id = $releaseId
    release_zip = $releaseZip
    sha256_before = $shaBefore
    sha256_after = $shaAfter
    build_release_exit_code = $buildExit
    build_release_parse_ok = ($buildJson -ne $null)
    build_release_ok = ($(if ($buildJson -ne $null) { [bool]$buildJson.ok } else { $false }))
    env = @{
      force_repack = "1"
      release_id_override = $releaseId
    }
    restore_final_report = $RestoreFinalReport
    restore_ok = $restoreOk
    pass = @{
      build_release_infra = $pass1
      zip_unchanged = $pass2
    }
  }

  Write-Text $chaosFinal (($report | ConvertTo-Json -Compress -Depth 30))
  Append-Event $chaosEvents $chaosRunId "done" @{ pass1=$pass1; pass2=$pass2; build_exit=$buildExit }

  if ($pass1 -and $pass2) {
    Emit-AndExit $report $RC_OK
  } else {
    Emit-AndExit $report $RC_FAIL
  }
}
catch {
  $msg = $_.Exception.Message

  # best-effort: still write chaos final_report if possible
  try {
    if ($chaosFinal -and $chaosRunDir) {
      Ensure-Dir $chaosEvidence
      $report = [ordered]@{
        schema = "chaos_file_lock_release_pack_v1"
        repo = $repoPath
        chaos_run_id = $chaosRunId
        target_run_id = $RunId
        pass = @{ build_release_infra=$false; zip_unchanged=$false }
        error = @{ kind="infra"; type="unhandled_exception"; message=$msg }
      }
      Write-Text $chaosFinal (($report | ConvertTo-Json -Compress -Depth 30))
      try { Append-Event $chaosEvents $chaosRunId "error" @{ message=$msg } } catch {}
      Emit-AndExit $report $RC_INFRA
    }
  } catch {}

  Emit-AndExit ([ordered]@{
    schema="chaos_file_lock_release_pack_v1"
    repo=$repoPath
    chaos_run_id=$chaosRunId
    target_run_id=$RunId
    pass=@{ build_release_infra=$false; zip_unchanged=$false }
    error=@{ kind="infra"; type="unhandled_exception"; message=$msg }
  }) $RC_INFRA
}
