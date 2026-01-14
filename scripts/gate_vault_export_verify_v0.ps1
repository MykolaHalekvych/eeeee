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

function Find-HashesJsonWithZipSha([string]$dir) {
  if (!(Test-Path $dir)) { return $null }
  $cands = @(
    Get-ChildItem -Path $dir -File -Filter "*.json" -ErrorAction SilentlyContinue
  )
  foreach ($c in $cands) {
    $o = Parse-JsonLast $c.FullName
    if ($null -ne (Get-JsonProp $o "zip_sha256")) { return $c.FullName }
  }
  $cands2 = @(
    Get-ChildItem -Path $dir -File -Filter "*.json" -Recurse -ErrorAction SilentlyContinue
  )
  foreach ($c in $cands2) {
    $o = Parse-JsonLast $c.FullName
    if ($null -ne (Get-JsonProp $o "zip_sha256")) { return $c.FullName }
  }
  return $null
}

function Find-HashesJsonMatchingZipSha([string]$dir, [string]$zipShaLower) {
  if (!(Test-Path $dir)) { return $null }
  $cands = @(
    Get-ChildItem -Path $dir -File -Filter "*.json" -Recurse -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending
  )
  foreach ($c in $cands) {
    $o = Parse-JsonLast $c.FullName
    $v = Get-JsonProp $o "zip_sha256"
    if ($v -and ([string]$v).ToLower() -eq $zipShaLower) { return $c.FullName }
  }
  return $null
}

function Normalize-ExistingPath([string]$p) {
  if ([string]::IsNullOrWhiteSpace($p)) { return $null }
  $p2 = AbsPath $p $foundryRoot
  if (Test-Path $p2) { return (Resolve-Path $p2).Path }
  return $null
}

function Find-FirstExistingPathInObject([object]$o, [string]$suffixLower) {
  if ($null -eq $o) { return $null }

  if ($o -is [string]) {
    $s = [string]$o
    if ($s.ToLower().EndsWith($suffixLower)) {
      $p = Normalize-ExistingPath $s
      if ($p) { return $p }
    }
    return $null
  }

  if ($o -is [System.Collections.IDictionary]) {
    foreach ($k in $o.Keys) {
      $r = Find-FirstExistingPathInObject $o[$k] $suffixLower
      if ($r) { return $r }
    }
    return $null
  }

  if ($o -is [System.Collections.IEnumerable] -and -not ($o -is [string])) {
    foreach ($it in $o) {
      $r = Find-FirstExistingPathInObject $it $suffixLower
      if ($r) { return $r }
    }
    return $null
  }

  foreach ($p in $o.PSObject.Properties) {
    $r = Find-FirstExistingPathInObject $p.Value $suffixLower
    if ($r) { return $r }
  }
  return $null
}

function Find-ExistingPathInJsonFiles([string]$dir, [string]$suffixLower) {
  if (!(Test-Path $dir)) { return $null }
  $files = @(
    Get-ChildItem -Path $dir -File -Filter "*.json" -Recurse -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTime -Descending
  )
  foreach ($f in $files) {
    $o = Parse-JsonLast $f.FullName
    if ($null -eq $o) { continue }
    $p = Find-FirstExistingPathInObject $o $suffixLower
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

  # Resolve target run dir
  $targetDirAbs = Find-TargetRunDir $TargetRunId
  if (-not $targetDirAbs) { Infra "TargetRunId dir not found under args\data\smoke: $TargetRunId" $null }

  # Prefer hashes-from-run (ties to TargetRunId)
  $hashFromRun = Find-HashesJsonWithZipSha $targetDirAbs
  $expectedSha = $null
  if ($hashFromRun) {
    $o = Parse-JsonLast $hashFromRun
    $v = Get-JsonProp $o "zip_sha256"
    if ($v) { $expectedSha = ([string]$v).ToLower() }
  }

  # Resolve zip
  $srcZipAbs = Find-BundleZip $targetDirAbs

  if (-not $srcZipAbs) {
    $srcZipAbs = Find-ExistingPathInJsonFiles $targetDirAbs ".zip"
  }

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
    Infra "No zip for TargetRunId (run dir has no zip and resolution failed)" @{
      target_run_dir = $targetDirAbs
      hashes_in_run = $hashFromRun
      expected_sha = $expectedSha
      dist_root = $distRoot
      dist_zip_scan_limit = $DistZipScanLimit
    }
  }

  $zipSha = Sha256Lower $srcZipAbs

  # Resolve hashes strictly matching this zip sha
  $srcHashesAbs = Find-HashesJsonMatchingZipSha $targetDirAbs $zipSha
  if (-not $srcHashesAbs) { $srcHashesAbs = Find-HashesJsonMatchingZipSha (Split-Path $srcZipAbs -Parent) $zipSha }
  if (-not $srcHashesAbs) { $srcHashesAbs = Find-HashesJsonMatchingZipSha $distRoot $zipSha }
  if (-not $srcHashesAbs) { $srcHashesAbs = Find-ExistingPathInJsonFiles $targetDirAbs ".hashes.json" }

  if (-not $srcHashesAbs -or -not (Test-Path $srcHashesAbs)) {
    Infra "No hashes json matching zip_sha256 for resolved zip" @{
      target_run_dir = $targetDirAbs
      source_zip_path = $srcZipAbs
      zip_sha256 = $zipSha
      hashes_in_run = $hashFromRun
      expected_sha = $expectedSha
      dist_root = $distRoot
    }
  }

  # Step out dirs (avoid any collisions)
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
  }

  if ($ing.rc -eq 2) {
    Infra "Vault ingest INFRA" @{
      target_run_dir = $targetDirAbs
      source_zip_path = $srcZipAbs
      source_hashes_path = $srcHashesAbs
      zip_sha256 = $zipSha
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
      zip_sha256 = $zipSha
      ingest_rc=$ing.rc; ingest_stdout=$ing.stdout; ingest_stderr=$ing.stderr
    }
  }

  $vaultId = Get-JsonProp $ing.parsed "vault_id"
  if (-not $vaultId) {
    Infra "vault_ingest_v0 did not return vault_id" @{
      ingest_stdout = $ing.stdout
      ingest_stderr = $ing.stderr
    }
  }

  # 2) export
  $exp = Run-Step "export" $exportPs1 @{
    OutDir  = $stepExportOut
    VaultId = $vaultId
  }

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

  $zipPath = Get-JsonProp $exp.parsed "exported_zip"
  $hashesPath = Get-JsonProp $exp.parsed "exported_hashes"

  if (-not $zipPath) {
    $z = Get-ChildItem -Path $stepExportOut -File -Filter "*.zip" -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($z) { $zipPath = $z.FullName }
  }

  if (-not $hashesPath) {
    $h = Find-HashesJsonWithZipSha $stepExportOut
    if ($h) { $hashesPath = $h }
  }

  if (-not $zipPath -or -not (Test-Path $zipPath)) { Infra "Export did not produce zip to verify" @{ step_export_out=$stepExportOut } }
  if (-not $hashesPath -or -not (Test-Path $hashesPath)) { Infra "Export did not produce hashes to verify" @{ step_export_out=$stepExportOut } }

  $zipToVerify = $zipPath
  if ($ChaosCorruptZipBeforeVerify -eq "YES") {
    $corrupt = Join-Path $OutDirAbs ("CORRUPT_" + (Split-Path $zipPath -Leaf))
    Copy-Item $zipPath $corrupt -Force
    Add-Content -Path $corrupt -Value ([byte[]](0x00)) -Encoding Byte
    $zipToVerify = $corrupt
  }

  # 3) verify
  $ver = Run-Step "verify" $verifyPs1 @{
    OutDir     = $stepVerifyOut
    ZipPath    = $zipToVerify
    HashesPath = $hashesPath
  }

  $overall = 0
  if ($ver.rc -eq 2) { $overall = 2 }
  elseif ($ver.rc -eq 1) { $overall = 1 }

  $s = $base.Clone()
  $s.target_run_dir = $targetDirAbs
  $s.hashes_in_run = $hashFromRun
  $s.expected_sha = $expectedSha

  $s.source_zip_path = $srcZipAbs
  $s.source_hashes_path = $srcHashesAbs
  $s.source_zip_sha256 = $zipSha

  $s.vault_id = $vaultId

  $s.ingest_rc=$ing.rc; $s.export_rc=$exp.rc; $s.verify_rc=$ver.rc
  $s.ingest_stdout=$ing.stdout; $s.export_stdout=$exp.stdout; $s.verify_stdout=$ver.stdout
  $s.ingest_stderr=$ing.stderr; $s.export_stderr=$exp.stderr; $s.verify_stderr=$ver.stderr

  $s.step_ingest_out = $stepIngestOut
  $s.step_export_out = $stepExportOut
  $s.step_verify_out = $stepVerifyOut

  $s.export_zip_path = $zipPath
  $s.verify_zip_path = $zipToVerify
  $s.hashes_path = $hashesPath

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
