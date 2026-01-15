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

function NormPath([string]$s){
  if ($null -eq $s) { return "" }
  $x = $s.ToString().Trim().Replace("\","/")
  while($x.StartsWith("./")){ $x = $x.Substring(2) }
  return $x
}

# glob(**,*) -> -like pattern
function GlobToLike([string]$glob){
  $g = NormPath $glob
  if([string]::IsNullOrWhiteSpace($g)) { return "" }
  $g = $g.Replace("[","`[").Replace("]","`]")
  $g = $g.Replace("**","*")
  return $g
}

function MatchesAny([string]$path, $patterns){
  foreach($p in $patterns){
    $pp = [string]$p
    if([string]::IsNullOrWhiteSpace($pp)) { continue }
    $like = GlobToLike $pp
    if($like -and ($path -like $like)) { return $true }
  }
  return $false
}

function NextBaselineId([string]$oldId){
  $m = [regex]::Match($oldId, "(\d+)$")
  if(-not $m.Success){ throw "baseline_id format invalid: $oldId" }
  $n = [int]$m.Groups[1].Value
  $pad = $m.Groups[1].Value.Length
  $next = ($n + 1).ToString().PadLeft($pad,'0')
  return $oldId.Substring(0, $oldId.Length - $m.Groups[1].Value.Length) + $next
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

  $oldBaselinePath = Join-Path $baseAbs ("$oldId\drift_baseline_$oldId.json")
  if(-not (Test-Path -LiteralPath $oldBaselinePath)){
    Emit $false $RC_INFRA "INFRA_BASELINE_FILE_MISSING" @{baseline_id=$oldId; baseline_path=$oldBaselinePath} $null
  }

  $newId = NextBaselineId $oldId
  $newDir = Join-Path $baseAbs $newId
  Ensure-Dir $newDir
  $newBaselinePath = Join-Path $newDir ("drift_baseline_$newId.json")

  # policy include/exclude
  $include = @()
  if($policy.PSObject.Properties.Name -contains "include"){ $include = $policy.include }
  $exclude = @()
  if($policy.PSObject.Properties.Name -contains "exclude"){ $exclude = $policy.exclude }

  if(($include | Measure-Object).Count -eq 0){
    Emit $false $RC_INFRA "INFRA_POLICY_INCLUDE_EMPTY" @{policy=$policyAbs} $null
  }

  # Save old policy id for rollback
  $oldPolicyId = [string]$policy.baseline_id

  # UPDATE policy baseline_id BEFORE hashing (so policy file hash matches)
  $policy.baseline_id = $newId
  WriteJson $policyAbs $policy

  try {
    # Scan only include roots on disk (captures untracked/ignored in scope)
    $roots = New-Object System.Collections.Generic.List[string]
    $rootFiles = New-Object System.Collections.Generic.List[string]

    foreach($pat in $include){
      $p = NormPath ([string]$pat)
      if([string]::IsNullOrWhiteSpace($p)) { continue }

      if($p.Contains("/")){
        $seg = $p.Split("/")[0]
        if($seg -and -not ($roots.Contains($seg))){ $roots.Add($seg) }
      } else {
        # root file like control_plane.json, .gitignore, etc.
        if(-not ($rootFiles.Contains($p))){ $rootFiles.Add($p) }
      }
    }

    $candidates = New-Object System.Collections.Generic.List[object]

    foreach($r in $roots){
      $dirAbs = Join-Path $repoAbs ($r -replace "/","\")
      if(Test-Path -LiteralPath $dirAbs){
        Get-ChildItem -LiteralPath $dirAbs -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object {
          $candidates.Add($_)
        }
      }
    }

    foreach($f in $rootFiles){
      $fAbs = Join-Path $repoAbs ($f -replace "/","\")
      if(Test-Path -LiteralPath $fAbs){
        $candidates.Add((Get-Item -LiteralPath $fAbs))
      }
    }

    if($candidates.Count -eq 0){
      throw "No candidate files found from include roots"
    }

    # Apply policy include/exclude to candidate file list
    $entries = New-Object System.Collections.Generic.List[object]
    $seen = @{}

    foreach($fi in $candidates){
      $full = [string]$fi.FullName
      if(-not ($full.StartsWith($repoAbs))) { continue }

      $rel = $full.Substring($repoAbs.Length).TrimStart("\")
      $rel = $rel.Replace("\","/")
      if([string]::IsNullOrWhiteSpace($rel)) { continue }

      if($seen.ContainsKey($rel)) { continue }
      $seen[$rel] = $true

      if(-not (MatchesAny $rel $include)) { continue }
      if(MatchesAny $rel $exclude) { continue }

      $h = (Get-FileHash -Algorithm SHA256 -LiteralPath $full).Hash.ToLower()
      $entries.Add([ordered]@{
        path   = $rel
        sha256 = $h
        bytes  = [int64]$fi.Length
      })
    }

    if($entries.Count -eq 0){
      throw "No files after policy scope (include/exclude)"
    }

    # Template baseline (keep schema)
    $tpl = ReadJson $oldBaselinePath
    if($tpl.PSObject.Properties.Name -contains "baseline_id"){ $tpl.baseline_id = $newId }
    if($tpl.PSObject.Properties.Name -contains "baselineId"){ $tpl.baselineId = $newId }
    if($tpl.PSObject.Properties.Name -contains "ts_utc"){ $tpl.ts_utc = (UtcTs) }
    if($tpl.PSObject.Properties.Name -contains "created_utc"){ $tpl.created_utc = (UtcTs) }

    if($tpl.PSObject.Properties.Name -contains "files"){
      $tpl.files = $entries
    } elseif($tpl.PSObject.Properties.Name -contains "items"){
      $tpl.items = $entries
    } elseif($tpl.PSObject.Properties.Name -contains "entries"){
      $tpl.entries = $entries
    } else {
      Add-Member -InputObject $tpl -NotePropertyName "files" -NotePropertyValue $entries -Force
    }

    WriteJson $newBaselinePath $tpl

    Emit $true $RC_OK "OK" @{
      old_baseline_id=$oldId
      new_baseline_id=$newId
      old_baseline_path=$oldBaselinePath
      new_baseline_path=$newBaselinePath
      scoped_files=$entries.Count
      include=$include
      exclude=$exclude
      policy_path=$policyAbs
    } $newDir
  }
  catch {
    # rollback policy baseline_id on failure
    try {
      $policy2 = ReadJson $policyAbs
      $policy2.baseline_id = $oldPolicyId
      WriteJson $policyAbs $policy2
    } catch {}
    throw
  }
}
catch{
  Emit $false $RC_INFRA "INFRA_TOOL_ERROR" @{error=$_.Exception.Message} $null
}
