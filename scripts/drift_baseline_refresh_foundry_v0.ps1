param(
  [ValidateSet("YES","NO")][string]$BaselineAcceptance = "NO",
  [string]$RepoRoot = ".",
  [string]$PolicyPath = ".\args\configs\drift_policy_foundry_v0.json",
  [string]$BaselinesRoot = ".\baselines\drift"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RC_OK=0; $RC_FAIL=1; $RC_INFRA=2
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function UtcTs(){ [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ") }
function Ensure-Dir([string]$p){ if($p){ New-Item -ItemType Directory -Force -Path $p | Out-Null } }
function NormalizeRc([int]$rc){ if($rc -in 0,1,2){$rc}else{2} }

function ReadTextAutoBom([string]$path){
  if(-not (Test-Path -LiteralPath $path)){ throw "Missing file: $path" }
  $sr = New-Object System.IO.StreamReader($path, [System.Text.Encoding]::UTF8, $true)
  try { $sr.ReadToEnd() } finally { $sr.Close() }
}
function ReadJson([string]$path){
  $raw = ReadTextAutoBom $path
  if([string]::IsNullOrWhiteSpace($raw)){ throw "Empty JSON: $path" }
  return ($raw | ConvertFrom-Json)
}
function WriteTextUtf8NoBom([string]$path,[string]$text){
  $dir = Split-Path -Parent $path
  Ensure-Dir $dir
  $norm = ($text -replace "`r`n","`n")
  [System.IO.File]::WriteAllText($path, $norm, $Utf8NoBom)
}
function WriteJson([string]$path,$obj){
  $json = ($obj | ConvertTo-Json -Depth 40)
  WriteTextUtf8NoBom $path $json
}

function Emit([bool]$ok,[int]$rc,[string]$reason,$detail,[string]$outDir){
  $rc = NormalizeRc $rc
  $obj = [ordered]@{
    schema="drift_baseline_refresh_foundry_v0"
    ok=$ok
    exit_code=$rc
    reason_code=$reason
    ts_utc=(UtcTs)
    repo_root=(Resolve-Path -LiteralPath $RepoRoot).Path
    policy_path=(Resolve-Path -LiteralPath $PolicyPath).Path
    baselines_root=(Resolve-Path -LiteralPath $BaselinesRoot).Path
    out_dir=$outDir
    details=$detail
  }
  if($outDir){
    Ensure-Dir $outDir
    WriteJson (Join-Path $outDir "summary.json") $obj
  }
  Write-Output ($obj | ConvertTo-Json -Depth 40 -Compress)
  exit $rc
}

try{
  $repoAbs = (Resolve-Path -LiteralPath $RepoRoot).Path
  $policyAbs = (Resolve-Path -LiteralPath $PolicyPath).Path
  Ensure-Dir $BaselinesRoot
  $baseAbs = (Resolve-Path -LiteralPath $BaselinesRoot).Path

  if($BaselineAcceptance -ne "YES"){
    Emit $false $RC_FAIL "DENY_BASELINE_ACCEPTANCE_REQUIRED" @{hint="Run with -BaselineAcceptance YES"} $null
  }

  $policy = ReadJson $policyAbs
  $oldId = [string]$policy.baseline_id
  if([string]::IsNullOrWhiteSpace($oldId)){
    Emit $false $RC_INFRA "INFRA_BASELINE_ID_MISSING" @{policy=$policyAbs} $null
  }

  $oldPath = Join-Path $baseAbs ("$oldId\drift_baseline_$oldId.json")
  if(-not (Test-Path -LiteralPath $oldPath)){
    Emit $false $RC_INFRA "INFRA_BASELINE_FILE_MISSING" @{baseline_id=$oldId; baseline_path=$oldPath} $null
  }

  # next id: increment last digits
  $m = [regex]::Match($oldId, "(\d+)$")
  if(-not $m.Success){
    Emit $false $RC_INFRA "INFRA_BASELINE_ID_FORMAT" @{baseline_id=$oldId; expected="...0004"} $null
  }
  $n = [int]$m.Groups[1].Value
  $nextN = $n + 1
  $pad = $m.Groups[1].Value.Length
  $nextSuffix = $nextN.ToString().PadLeft($pad,'0')
  $newId = $oldId.Substring(0, $oldId.Length - $m.Groups[1].Value.Length) + $nextSuffix

  $newDir = Join-Path $baseAbs $newId
  Ensure-Dir $newDir
  $newPath = Join-Path $newDir ("drift_baseline_$newId.json")

  # file list from git ls-files (deterministic)
  Push-Location $repoAbs
  try{
    $paths = (& git ls-files) 2>$null
    if($LASTEXITCODE -ne 0){ throw "git ls-files failed" }
  } finally { Pop-Location }

  $paths = $paths | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" } | Sort-Object
  if($paths.Count -eq 0){
    Emit $false $RC_INFRA "INFRA_NO_TRACKED_FILES" @{repo=$repoAbs} $null
  }

  # entries require bytes (Drift Detector expects it)
  $entries = New-Object System.Collections.Generic.List[object]
  foreach($p in $paths){
    $abs = Join-Path $repoAbs ($p -replace "/","\")
    if(-not (Test-Path -LiteralPath $abs)){ continue }

    $item = Get-Item -LiteralPath $abs
    $h = (Get-FileHash -Algorithm SHA256 -LiteralPath $abs).Hash.ToLower()

    $entries.Add([ordered]@{
      path   = ($p -replace "\\","/")
      sha256 = $h
      bytes  = [int64]$item.Length
    })
  }

  # load old baseline as template to keep schema stable
  $tpl = ReadJson $oldPath

  # set baseline id fields if present
  if($tpl.PSObject.Properties.Name -contains "baseline_id"){ $tpl.baseline_id = $newId }
  if($tpl.PSObject.Properties.Name -contains "baselineId"){ $tpl.baselineId = $newId }
  if($tpl.PSObject.Properties.Name -contains "ts_utc"){ $tpl.ts_utc = (UtcTs) }
  if($tpl.PSObject.Properties.Name -contains "created_utc"){ $tpl.created_utc = (UtcTs) }

  # replace file list (support common names)
  if($tpl.PSObject.Properties.Name -contains "files"){
    $tpl.files = $entries
  } elseif($tpl.PSObject.Properties.Name -contains "items"){
    $tpl.items = $entries
  } elseif($tpl.PSObject.Properties.Name -contains "entries"){
    $tpl.entries = $entries
  } else {
    Add-Member -InputObject $tpl -NotePropertyName "files" -NotePropertyValue $entries -Force
  }

  WriteJson $newPath $tpl

  # update policy baseline_id
  $policy.baseline_id = $newId
  WriteJson $policyAbs $policy

  Emit $true $RC_OK "OK" @{
    old_baseline_id=$oldId
    new_baseline_id=$newId
    old_baseline_path=$oldPath
    new_baseline_path=$newPath
    tracked_files=$paths.Count
    hashed_files=$entries.Count
    policy_path=$policyAbs
  } $newDir
}
catch{
  Emit $false $RC_INFRA "INFRA_TOOL_ERROR" @{error=$_.Exception.Message} $null
}
