param(
  [Parameter(Mandatory=$true)][string]$RunId,
  [Parameter(Mandatory=$true)][string]$OutDir,
  [Parameter(Mandatory=$true)][string]$VaultRepo,     # C:\Users\mukol\ARGS-Evidence-Vault-v0
  [Parameter(Mandatory=$true)][string]$TargetRunId,   # dir name under args\data\smoke\...
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][ValidateSet("NO","YES")][string]$ChaosCorruptZipBeforeVerify = "NO",
  [Parameter(Mandatory=$false)][int]$DistZipScanLimit = 200
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

function Sha256Lower([string]$path) {
  return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
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
  run_acceptance = $RunAcceptance
  chaos_corrupt_zip_before_verify = $ChaosCorruptZipBeforeVerify
  dist_zip_scan_limit = $DistZipScanLimit
  ok          = $false
  exit_code   = 2
  result      = "INFRA"
  reason_code = "INFRA"
}

function Emit([hashtable]$s, [int]$rc) {
  Write-JsonFile $summaryPath $s
  Write-Json $s
  exit $rc
}

function Infra([string]$msg, [hashtable]$extra) {
  $s = $base.Clone()
  $s.ok = $false
  $s.exit_code = 2
  $s.result = "INFRA"
  $s.reason_code = "INFRA"
  $s.error = $msg
  if ($extra) { foreach ($kv in $extra.GetEnumerator()) { $s[$kv.Key] = $kv.Value } }
  Emit $s 2
}

function Fail([string]$reason, [hashtable]$extra) {
  $s = $base.Clone()
  $s.ok = $false
  $s.exit_code = 1
  $s.result = "FAIL"
  $s.reason_code = $reason
  if ($extra) { foreach ($kv in $extra.GetEnumerator()) { $s[$kv.Key] = $kv.Value } }
  Emit $s 1
}

function Find-TargetRunDir([string]$runId) {
  $smokeRoot = Join-Path $foundryRoot "args\data\smoke"
  if (!(Test-Path $smokeRoot)) { return $null }
  $cands = @(
    Get-ChildItem -Path $smokeRoot -Directory -Recurse -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -eq $runId } |
      Sort-Object LastWriteTime -Descending
  )
  if ($cands.Count -ge 1) { return $cands[0].FullName }
  return $null
}

function Find-BundleZip([string]$dir) {
  if (!(Test-Path $dir)) { return $null }
  $z = Get-ChildItem -Path $dir -File -Filter "*.zip" -ErrorAction SilentlyContinue |
        Sort-Object Length -Descending | Select-Object -First 1
  if ($z) { return $z.FullName }
  $z2 = Get-ChildItem -Path $dir -File -Filter "*.zip" -Recurse -ErrorAction SilentlyContinue |
         Sort-Object Length -Descending | Select-Object -First 1
  if ($z2) { return $z2.FullName }
  return $null
}

# recursively find a key (e.g. zip_sha256) anywhere in an object
function Find-KeyValueInObject([object]$o, [string]$keyLower) {
  if ($null -eq $o) { return $null }

  if ($o -is [System.Collections.IDictionary]) {
    foreach ($k in $o.Keys) {
      if ($k -and ([string]$k).ToLower() -eq $keyLower) {
        $v = $o[$k]
        if ($null -ne $v) { return [string]$v }
      }
      $r = Find-KeyValueInObject $o[$k] $keyLower
      if ($r) { return $r }
    }
    return $null
  }

  if ($o -is [System.Collections.IEnumerable] -and -not ($o -is [string])) {
    foreach ($it in $o) {
      $r = Find-KeyValueInObject $it $keyLower
      if ($r) { return $r }
    }
    return $null
  }

  foreach ($p in $o.PSObject.Properties) {
    if ($p.Name -and $p.Name.ToLower() -eq $keyLower) {
      if ($null -ne $p.Value) { return [string]$p.Value }
    }
    $r = Find-KeyValueInObject $p.Value $keyLower
    if ($r) { return $r }
  }

  return $null
}

function Find-KeyValueInJsonFiles([string]$dir, [string]$keyLower) {
  if (!(Test-Path $dir)) { return $null }
  $files = @(
    Get-ChildItem -Path $dir -File -Filter "*.json" -Recurse -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending
  )
  foreach ($f in $files) {
    $o = Parse-JsonLast $f.FullName
    if ($null -eq $o) { continue }
    $v = Find-KeyValueInObject $o $keyLower
    if ($v) { return $v }
  }
  return $null
}

function Normalize-ExistingPathWithBases([string]$p, [string[]]$bases) {
  if ([string]::IsNullOrWhiteSpace($p)) { return $null }

  if ([System.IO.Path]::IsPathRooted($p)) {
    if (Test-Path $p) { return (Resolve-Path $p).Path }
    return $null
  }

  foreach ($b in $bases) {
    if ([string]::IsNullOrWhiteSpace($b)) { continue }
    $pp = Join-Path $b $p
    if (Test-Path $pp) { return (Resolve-Path $pp).Path }
  }
  return $null
}

function Find-FirstExistingPathInObject([object]$o, [string]$suffixLower, [string[]]$bases) {
  if ($null -eq $o) { return $null }

  if ($o -is [string]) {
    $s = [string]$o
    if ($s.ToLower().EndsWith($suffixLower)) {
      $p = Normalize-ExistingPathWithBases $s $bases
      if ($p) { return $p }
    }
    return $null
  }

  if ($o -is [System.Collections.IDictionary]) {
    foreach ($k in $o.Keys) {
      $r = Find-FirstExistingPathInObject $o[$k] $suffixLower $bases
      if ($r) { return $r }
    }
    return $null
  }

  if ($o -is [System.Collections.IEnumerable] -and -not ($o -is [string])) {
    foreach ($it in $o) {
      $r = Find-FirstExistingPathInObject $it $suffixLower $bases
      if ($r) { return $r }
    }
    return $null
  }

  foreach ($p in $o.PSObject.Properties) {
    $r = Find-FirstExistingPathInObject $p.Value $suffixLower $bases
    if ($r) { return $r }
  }
  return $null
}

function Find-ExistingPathInJsonFiles([string]$dir, [string]$suffixLower, [string[]]$bases) {
  if (!(Test-Path $dir)) { return $null }
  $files = @(
    Get-ChildItem -Path $dir -File -Filter "*.json" -Recurse -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending
  )
  foreach ($f in $files) {
    $o = Parse-JsonLast $f.FullName
    if ($null -eq $o) { continue }
    $p = Find-FirstExistingPathInObject $o $suffixLower $bases
    if ($p) { return $p }
  }
  return $null
}

function Find-ZipBySha([string]$dir, [string]$zipShaLower, [int]$limit) {
  if (!(Test-Path $dir)) { return $null }
  $cands = @(
    Get-ChildItem -Path $dir -File -Filter "*.zip" -Recurse -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending
  )
  $n = [Math]::Min($limit, $cands.Count)
  for ($i=0; $i -lt $n; $i++) {
    $p = $cands[$i].FullName
    try {
      $sha = Sha256Lower $p
      if ($sha -eq $zipShaLower) { return $p }
    } catch { }
  }
  return $null
}

function Find-HashesJsonMatchingZipShaTopLevel([string]$dir, [string]$zipShaLower) {
  if (!(Test-Path $dir)) { return $null }
  $cands = @(
    Get-ChildItem -Path $dir -File -Filter "*.json" -Recurse -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending
  )
  foreach ($c in $cands) {
    $o = Parse-JsonLast $c.FullName
    $v = Get-JsonProp $o "zip_sha256"   # MUST be top-level for Vault shim
    if ($v -and ([string]$v).ToLower() -eq $zipShaLower) { return $c.FullName }
  }
  return $null
}

function Run-Step([string]$name, [string]$ps1, [hashtable]$invoke, [string]$workingDir) {
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
    -WorkingDirectory $workingDir `
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

try {
  if ($RunAcceptance -ne "YES") {
    Fail "RUN_ACCEPTANCE_REQUIRED" @{ run_acceptance = $RunAcceptance }
  }

  if (!(Test-Path $VaultRepo)) { Infra "VaultRepo not found: $VaultRepo" $null }

  $ingestPs1 = Join-Path $VaultRepo "scripts\vault_ingest_v0.ps1"
  $exportPs1 = Join-Path $VaultRepo "scripts\vault_export_v0.ps1"
  $verifyPs1 = Join-Path $VaultRepo "scripts\vault_verify_v0.ps1"

  if (!(Test-Path $ingestPs1)) { Infra "Missing: $ingestPs1" $null }
  if (!(Test-Path $exportPs1)) { Infra "Missing: $exportPs1" $null }
  if (!(Test-Path $verifyPs1)) { Infra "Missing: $verifyPs1" $null }

  $distRoot = Join-Path $foundryRoot "dist"

  $targetDirAbs = Find-TargetRunDir $TargetRunId
  if (-not $targetDirAbs) { Infra "TargetRunId dir not found under args\data\smoke: $TargetRunId" $null }

  # Bases for resolving relative paths inside run JSON:
  $bases = @($targetDirAbs, $foundryRoot)

  # Extract expected sha from ANY json (even nested)
  $expectedSha = Find-KeyValueInJsonFiles $targetDirAbs "zip_sha256"
  if ($expectedSha) { $expectedSha = ([string]$expectedSha).ToLower() }

  # Try find zip:
  $zipPathFromJson = Find-ExistingPathInJsonFiles $targetDirAbs ".zip" $bases
  $srcZipAbs = Find-BundleZip $targetDirAbs
  if (-not $srcZipAbs -and $zipPathFromJson) { $srcZipAbs = $zipPathFromJson }

  if ($srcZipAbs -and $expectedSha) {
    try {
      $sha = Sha256Lower $srcZipAbs
      if ($sha -ne $expectedSha) { $srcZipAbs = $null }
    } catch { $srcZipAbs = $null }
  }

  if (-not $srcZipAbs -and $expectedSha) {
    $srcZipAbs = Find-ZipBySha $distRoot $expectedSha $DistZipScanLimit
  }

  if (-not $srcZipAbs) {
    Infra "No zip for TargetRunId (resolution failed)" @{
      target_run_dir = $targetDirAbs
      dist_root = $distRoot
      dist_zip_scan_limit = $DistZipScanLimit
      zip_path_from_json = $zipPathFromJson
      expected_sha = $expectedSha
    }
  }

  $zipSha = Sha256Lower $srcZipAbs

  # Find real hashes file (must have TOP-LEVEL zip_sha256 for Vault shim)
  $hashesPathFromJson = Find-ExistingPathInJsonFiles $targetDirAbs ".hashes.json" $bases

  $srcHashesAbs = Find-HashesJsonMatchingZipShaTopLevel $targetDirAbs $zipSha
  if (-not $srcHashesAbs) { $srcHashesAbs = Find-HashesJsonMatchingZipShaTopLevel (Split-Path $srcZipAbs -Parent) $zipSha }
  if (-not $srcHashesAbs) { $srcHashesAbs = Find-HashesJsonMatchingZipShaTopLevel $distRoot $zipSha }
  if (-not $srcHashesAbs -and $hashesPathFromJson) { $srcHashesAbs = $hashesPathFromJson }

  $hashesGenerated = $false
  if (-not $srcHashesAbs -or -not (Test-Path $srcHashesAbs)) {
    # last resort: generate minimal hashes for Vault bridge validation
    $gen = Join-Path $OutDirAbs "hashes_generated_vault_gate.json"
    $obj = @{
      schema="release_hashes_v0"
      ts_utc=$ts
      zip_sha256=$zipSha
      generated_by="gate_vault_export_verify_v0"
    }
    Set-Content -Path $gen -Value ($obj | ConvertTo-Json -Compress -Depth 10) -Encoding UTF8
    $srcHashesAbs = $gen
    $hashesGenerated = $true
  }

  # Step out dirs
  $stepIngestOut = Join-Path $OutDirAbs "step_ingest"
  $stepExportOut = Join-Path $OutDirAbs "step_export"
  $stepVerifyOut = Join-Path $OutDirAbs "step_verify"
  Ensure-Dir $stepIngestOut
  Ensure-Dir $stepExportOut
  Ensure-Dir $stepVerifyOut

  # 1) ingest
  $ing = Run-Step "ingest" $ingestPs1 @{
    OutDir     = $stepIngestOut
    ZipPath    = $srcZipAbs
    HashesPath = $srcHashesAbs
  } $VaultRepo

  if ($ing.rc -eq 2) {
    Infra "Vault ingest INFRA" @{
      target_run_dir = $targetDirAbs
      source_zip_path = $srcZipAbs
      source_hashes_path = $srcHashesAbs
      source_zip_sha256 = $zipSha
      hashes_generated = $hashesGenerated
      ingest_rc=$ing.rc; ingest_stdout=$ing.stdout; ingest_stderr=$ing.stderr
    }
  }
  if ($ing.rc -eq 1) {
    $reason = Get-JsonProp $ing.parsed "reason_code"
    if (-not $reason) { $reason = "DENY_INGEST" }
    Fail $reason @{
      target_run_dir = $targetDirAbs
      source_zip_path = $srcZipAbs
      source_hashes_path = $srcHashesAbs
      source_zip_sha256 = $zipSha
      hashes_generated = $hashesGenerated
      ingest_rc=$ing.rc; ingest_stdout=$ing.stdout; ingest_stderr=$ing.stderr
    }
  }

  $vaultId = Get-JsonProp $ing.parsed "vault_id"
  if (-not $vaultId) {
    Infra "vault_ingest_v0 did not return vault_id" @{
      ingest_stdout=$ing.stdout; ingest_stderr=$ing.stderr
    }
  }

  # 2) export
  $exp = Run-Step "export" $exportPs1 @{
    OutDir  = $stepExportOut
    VaultId = $vaultId
  } $VaultRepo

  if ($exp.rc -eq 2) {
    Infra "Vault export INFRA" @{
      vault_id = $vaultId
      export_rc=$exp.rc; export_stdout=$exp.stdout; export_stderr=$exp.stderr
      ingest_rc=$ing.rc; ingest_stdout=$ing.stdout; ingest_stderr=$ing.stderr
    }
  }
  if ($exp.rc -eq 1) {
    $reason = Get-JsonProp $exp.parsed "reason_code"
    if (-not $reason) { $reason = "DENY_EXPORT" }
    Fail $reason @{
      vault_id=$vaultId
      export_rc=$exp.rc; export_stdout=$exp.stdout; export_stderr=$exp.stderr
      ingest_rc=$ing.rc; ingest_stdout=$ing.stdout; ingest_stderr=$ing.stderr
    }
  }

  $zipOut = Get-JsonProp $exp.parsed "exported_zip"
  $hashOut = Get-JsonProp $exp.parsed "exported_hashes"

  if (-not $zipOut) {
    $z = Get-ChildItem -Path $stepExportOut -File -Filter "*.zip" -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($z) { $zipOut = $z.FullName }
  }
  if (-not $hashOut) {
    $h = Get-ChildItem -Path $stepExportOut -File -Filter "*.json" -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($h) { $hashOut = $h.FullName }
  }

  if (-not $zipOut -or -not (Test-Path $zipOut)) { Infra "Export did not produce zip to verify" @{ step_export_out=$stepExportOut } }
  if (-not $hashOut -or -not (Test-Path $hashOut)) { Infra "Export did not produce hashes to verify" @{ step_export_out=$stepExportOut } }

  $zipToVerify = $zipOut
  if ($ChaosCorruptZipBeforeVerify -eq "YES") {
    $corrupt = Join-Path $OutDirAbs ("CORRUPT_" + (Split-Path $zipOut -Leaf))
    Copy-Item $zipOut $corrupt -Force
    Add-Content -Path $corrupt -Value ([byte[]](0x00)) -Encoding Byte
    $zipToVerify = $corrupt
  }

  # 3) verify
  $ver = Run-Step "verify" $verifyPs1 @{
    OutDir     = $stepVerifyOut
    ZipPath    = $zipToVerify
    HashesPath = $hashOut
  } $VaultRepo

  $overall = 0
  if ($ver.rc -eq 2) { $overall = 2 }
  elseif ($ver.rc -eq 1) { $overall = 1 }

  $s = $base.Clone()
  $s.target_run_dir = $targetDirAbs
  $s.dist_root = $distRoot
  $s.zip_path_from_json = $zipPathFromJson
  $s.hashes_path_from_json = $hashesPathFromJson
  $s.expected_sha = $expectedSha

  $s.source_zip_path = $srcZipAbs
  $s.source_hashes_path = $srcHashesAbs
  $s.source_zip_sha256 = $zipSha
  $s.hashes_generated = $hashesGenerated

  $s.vault_id = $vaultId

  $s.ingest_rc=$ing.rc; $s.export_rc=$exp.rc; $s.verify_rc=$ver.rc
  $s.ingest_stdout=$ing.stdout; $s.export_stdout=$exp.stdout; $s.verify_stdout=$ver.stdout
  $s.ingest_stderr=$ing.stderr; $s.export_stderr=$exp.stderr; $s.verify_stderr=$ver.stderr

  $s.step_ingest_out = $stepIngestOut
  $s.step_export_out = $stepExportOut
  $s.step_verify_out = $stepVerifyOut

  $s.export_zip_path = $zipOut
  $s.verify_zip_path = $zipToVerify
  $s.hashes_path = $hashOut

  if ($overall -eq 1) {
    $reason = Get-JsonProp $ver.parsed "reason_code"
    if (-not $reason) { $reason = "DENY_VERIFY" }
    $s.reason_code = $reason
  } else {
    $s.reason_code = $(if ($overall -eq 0) { "PASS" } else { "INFRA" })
  }

  $s.exit_code = $overall
  $s.ok = ($overall -eq 0)
  $s.result = $(if ($overall -eq 0) { "PASS" } elseif ($overall -eq 1) { "FAIL" } else { "INFRA" })

  Emit $s $overall

} catch {
  Infra ("UnhandledException: " + $_.Exception.Message) $null
}
