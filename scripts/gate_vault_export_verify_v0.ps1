param(
  [Parameter(Mandatory=$true)][string]$RunId,
  [Parameter(Mandatory=$true)][string]$OutDir,
  [Parameter(Mandatory=$true)][string]$VaultRepo,     # C:\Users\mukol\ARGS-Evidence-Vault-v0
  [Parameter(Mandatory=$true)][string]$TargetRunId,
  [Parameter(Mandatory=$false)][ValidateSet("NO","YES")][string]$ChaosCorruptZipBeforeVerify = "NO"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_gate_common.ps1"

$ts = ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"))
$foundryRoot = (Get-Location).Path

function AbsPath([string]$p, [string]$base) {
  if ([string]::IsNullOrWhiteSpace($p)) { return $p }
  if ([System.IO.Path]::IsPathRooted($p)) { return $p }
  return (Join-Path $base $p)
}

function Parse-JsonLast([string]$path) {
  if (!(Test-Path $path)) { return $null }
  $txt = (Get-Content -Raw -Encoding UTF8 $path).Trim()
  if ($txt.Length -eq 0) { return $null }
  try { return ($txt | ConvertFrom-Json) } catch { }
  $lines = @($txt -split "`r?`n" | Where-Object { $_.Trim().Length -gt 0 })
  for ($i = $lines.Length - 1; $i -ge 0; $i--) {
    $l = $lines[$i].Trim()
    if ($l.StartsWith("{") -and $l.EndsWith("}")) {
      try { return ($l | ConvertFrom-Json) } catch { }
    }
  }
  return $null
}

function Get-JsonProp([object]$o, [string]$name) {
  if ($null -eq $o) { return $null }
  $p = $o.PSObject.Properties[$name]
  if ($null -eq $p) { return $null }
  return $p.Value
}

$OutDirAbs = AbsPath $OutDir $foundryRoot
Ensure-Dir $OutDirAbs

$summaryPath = Join-Path $OutDirAbs "summary.json"

$base = @{
  schema      = "gate_vault_export_verify_v0"
  ts_utc      = $ts
  run_id      = $RunId
  target_run_id = $TargetRunId
  vault_repo  = $VaultRepo
  out_dir     = $OutDirAbs
  chaos_corrupt_zip_before_verify = $ChaosCorruptZipBeforeVerify

  ok          = $false
  exit_code   = 2
  result      = "INFRA"
}

function Emit([hashtable]$s, [int]$rc) {
  Write-JsonFile $summaryPath $s
  Write-Json $s
  exit $rc
}

function Infra([string]$msg) {
  $s = $base.Clone()
  $s.ok = $false
  $s.exit_code = 2
  $s.result = "INFRA"
  $s.error = $msg
  Emit $s 2
}

if (!(Test-Path $VaultRepo)) { Infra "VaultRepo not found: $VaultRepo" }

$ingestPs1 = Join-Path $VaultRepo "scripts\vault_ingest_v0.ps1"
$exportPs1 = Join-Path $VaultRepo "scripts\vault_export_v0.ps1"
$verifyPs1 = Join-Path $VaultRepo "scripts\vault_verify_v0.ps1"

if (!(Test-Path $ingestPs1)) { Infra "Missing: $ingestPs1" }
if (!(Test-Path $exportPs1)) { Infra "Missing: $exportPs1" }
if (!(Test-Path $verifyPs1)) { Infra "Missing: $verifyPs1" }

function Build-InvokeArgs([string]$ps1, [string]$targetRunId, [string]$outDir, [string]$zipPath, [string]$hashesPath) {
  $cmdInfo = Get-Command $ps1
  $keys = $cmdInfo.Parameters.Keys

  $invoke = @{}

  function MapArg([string[]]$names, [object]$val) {
    foreach ($n in $names) {
      if ($keys -contains $n) { $invoke[$n] = $val; return $true }
    }
    return $false
  }

  # target run id
  MapArg @("TargetRunId","TargetId","Target","RunId","SourceRunId") $targetRunId | Out-Null
  # out dir
  MapArg @("OutDir","Out","OutRoot","OutPath") $outDir | Out-Null

  # optional zip/hashes
  if ($zipPath)   { MapArg @("Zip","ZipPath","Bundle","BundlePath","ExportZip","ZipFile") $zipPath | Out-Null }
  if ($hashesPath){ MapArg @("Hashes","HashesPath","HashPath","HashesJsonPath","HashesFile") $hashesPath | Out-Null }

  return $invoke
}

function Run-Step([string]$name, [string]$ps1, [hashtable]$invoke) {
  $stdout = Join-Path $OutDirAbs ("vault_" + $name + ".stdout.txt")
  $stderr = Join-Path $OutDirAbs ("vault_" + $name + ".stderr.txt")

  $argList = @("-NoProfile","-ExecutionPolicy","Bypass","-File",$ps1)
  foreach ($kv in ($invoke.GetEnumerator() | Sort-Object Name)) {
    $argList += ("-" + $kv.Key)
    $argList += [string]$kv.Value
  }

  $p = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList $argList `
    -WorkingDirectory $VaultRepo `
    -NoNewWindow `
    -Wait `
    -PassThru `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError  $stderr

  $rc = [int]$p.ExitCode
  $parsed = Parse-JsonLast $stdout
  $ec = Get-JsonProp $parsed "exit_code"
  if ($null -ne $ec) { $rc = [int]$ec }

  return @{
    rc = $rc
    stdout = $stdout
    stderr = $stderr
    parsed = $parsed
  }
}

# 1) ingest
$ing = Run-Step "ingest" $ingestPs1 (Build-InvokeArgs $ingestPs1 $TargetRunId $OutDirAbs $null $null)
# short-circuit on INFRA
if ($ing.rc -eq 2) {
  $s = $base.Clone(); $s.exit_code=2; $s.result="INFRA"; $s.ok=$false
  $s.ingest_rc=$ing.rc; $s.ingest_stdout=$ing.stdout; $s.ingest_stderr=$ing.stderr
  Emit $s 2
}

# 2) export
$exp = Run-Step "export" $exportPs1 (Build-InvokeArgs $exportPs1 $TargetRunId $OutDirAbs $null $null)
if ($exp.rc -eq 2) {
  $s = $base.Clone(); $s.exit_code=2; $s.result="INFRA"; $s.ok=$false
  $s.ingest_rc=$ing.rc; $s.export_rc=$exp.rc
  $s.ingest_stdout=$ing.stdout; $s.export_stdout=$exp.stdout
  $s.ingest_stderr=$ing.stderr; $s.export_stderr=$exp.stderr
  Emit $s 2
}

# Try discover zip/hashes paths (from export JSON or filesystem)
$zipPath = $null
$hashesPath = $null
foreach ($k in @("zip_path","bundle_path","export_zip","zip","bundle")) {
  $v = Get-JsonProp $exp.parsed $k
  if ($v) { $zipPath = [string]$v; break }
}
foreach ($k in @("hashes_path","hashes","hash_path")) {
  $v = Get-JsonProp $exp.parsed $k
  if ($v) { $hashesPath = [string]$v; break }
}

if (-not $zipPath) {
  $z = Get-ChildItem -File $OutDirAbs -Filter "*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($z) { $zipPath = $z.FullName }
}
if (-not $hashesPath) {
  $h = Get-ChildItem -File $OutDirAbs -Filter "*.hashes.json" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($h) { $hashesPath = $h.FullName }
}

$zipToVerify = $zipPath

if ($ChaosCorruptZipBeforeVerify -eq "YES" -and $zipPath -and (Test-Path $zipPath)) {
  $corrupt = Join-Path $OutDirAbs ("CORRUPT_" + (Split-Path $zipPath -Leaf))
  Copy-Item $zipPath $corrupt -Force
  Add-Content -Path $corrupt -Value ([byte[]](0x00)) -Encoding Byte
  $zipToVerify = $corrupt
}

# 3) verify
$ver = Run-Step "verify" $verifyPs1 (Build-InvokeArgs $verifyPs1 $TargetRunId $OutDirAbs $zipToVerify $hashesPath)

# compute overall rc: INFRA wins, then FAIL
$overall = 0
foreach ($r in @($ing.rc,$exp.rc,$ver.rc)) {
  if ($r -eq 2) { $overall = 2; break }
  elseif ($r -eq 1) { $overall = 1 }
}

$s = $base.Clone()
$s.ingest_rc=$ing.rc; $s.export_rc=$exp.rc; $s.verify_rc=$ver.rc
$s.ingest_stdout=$ing.stdout; $s.export_stdout=$exp.stdout; $s.verify_stdout=$ver.stdout
$s.ingest_stderr=$ing.stderr; $s.export_stderr=$exp.stderr; $s.verify_stderr=$ver.stderr
$s.export_zip_path = $zipPath
$s.verify_zip_path = $zipToVerify
$s.hashes_path = $hashesPath

$s.exit_code = $overall
$s.ok = ($overall -eq 0)
$s.result = $(if ($overall -eq 0) { "PASS" } elseif ($overall -eq 1) { "FAIL" } else { "INFRA" })

Emit $s $overall
