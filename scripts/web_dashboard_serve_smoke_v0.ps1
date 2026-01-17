param(
  [string]$ReleaseZip = "",
  [string]$CustomerBoxRoot = "",
  [int]$Port = 17812,
  [string]$RunAcceptance = "YES",
  [string]$AutoBuildIfMissing = "YES",
  [string]$WebDashboardKitId = "kit_web_dashboard_v0"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ---------- helpers ----------
function UtcIso { return [DateTime]::UtcNow.ToString("o").Replace("+00:00","Z") }
function UtcId  { return [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") }
function RandHex8 { return -join (1..8 | ForEach-Object { "{0:x}" -f (Get-Random -Max 16) }) }

function EnsureDir([string]$p) {
  if (-not $p) { return }
  New-Item -ItemType Directory -Force -Path $p | Out-Null
}

function WriteJson([string]$path, $obj) {
  ($obj | ConvertTo-Json -Depth 50 -Compress) | Out-File -Encoding utf8 $path
}

function CurlToFile([string]$url, [string]$path) {
  & curl.exe -sS -i $url 2>&1 | Out-File -Encoding utf8 $path
}

function HttpCode([string]$path) {
  if (-not (Test-Path $path)) { return 0 }
  $line = (Get-Content $path -TotalCount 1)
  if ($line -match "HTTP/\S+\s+(\d+)") { return [int]$matches[1] }
  return 0
}

function EmitReportAndExit([hashtable]$report, [string]$outDir, [string]$evidenceDir) {
  $report.out_dir = $outDir
  $report.evidence_dir = $evidenceDir
  WriteJson (Join-Path $outDir "final_report.json") $report
  Write-Output ($report | ConvertTo-Json -Depth 50 -Compress)
  exit [int]$report.exit_code
}

# ---------- main ----------
$schema = "web_dashboard_serve_smoke_v0"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

$runId = "WEB_DASHBOARD_SERVE_SMOKE_" + (UtcId) + "_" + (RandHex8)
$outDir = Join-Path $repo (Join-Path "args\data\smoke\$schema" $runId)
$evidenceDir = Join-Path $outDir "evidence"
EnsureDir $evidenceDir

# repo-local defaults (portable)
if (-not $CustomerBoxRoot) {
  $CustomerBoxRoot = Join-Path $repo "out\customer_box"
}
EnsureDir $CustomerBoxRoot

# ensure dist layout exists (CI-safe)
$distDir = Join-Path $repo "dist"
$releasesDir = Join-Path $distDir "releases"
EnsureDir $distDir
EnsureDir $releasesDir

# run-acceptance gate
if ($RunAcceptance -ne "YES") {
  $r = @{
    schema=$schema; ts_utc=(UtcIso)
    ok=$false; exit_code=1
    reason_code="FAIL.RUN_ACCEPTANCE_REQUIRED"; child_reason_code="FAIL.RUN_ACCEPTANCE_REQUIRED"
    repo=$repo; run_id=$runId
  }
  EmitReportAndExit $r $outDir $evidenceDir
}

# ---- acquire ReleaseZip (auto-build if missing) ----
$buildObserved = $null

if (-not $ReleaseZip) {
  $latest = Get-ChildItem -Path $releasesDir -Filter "web_dashboard_v0__*.zip" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1

  if (-not $latest -and $AutoBuildIfMissing -eq "YES") {
    $buildScript = Join-Path $repo "scripts\demo_build_release_no_llm_smoke_v0.ps1"
    if (-not (Test-Path $buildScript)) {
      $r = @{
        schema=$schema; ts_utc=(UtcIso)
        ok=$false; exit_code=2
        reason_code="INFRA.MISSING_BUILD_SCRIPT"; child_reason_code="INFRA.MISSING_BUILD_SCRIPT"
        repo=$repo; run_id=$runId
        error=("Cannot find build script: " + $buildScript)
      }
      EmitReportAndExit $r $outDir $evidenceDir
    }

    $bStdout = Join-Path $evidenceDir "00_build_release.stdout.txt"
    $bStderr = Join-Path $evidenceDir "00_build_release.stderr.txt"

    & powershell -NoProfile -ExecutionPolicy Bypass -File $buildScript `
      -KitId $WebDashboardKitId -RunAcceptance "YES" 1> $bStdout 2> $bStderr

    $rc = $LASTEXITCODE
    $buildObserved = @{
      rc = $rc
      script = $buildScript
      kit_id = $WebDashboardKitId
      stdout_path = $bStdout
      stderr_path = $bStderr
    }

    # try parse last JSON object from stdout
    try {
      $lines = Get-Content $bStdout -ErrorAction SilentlyContinue
      $jsonLine = ($lines | Where-Object { $_ -match '^\s*\{.*\}\s*$' } | Select-Object -Last 1)
      if ($jsonLine) {
        $buildObserved.json = ($jsonLine | ConvertFrom-Json)
      }
    } catch {
      $buildObserved.parse_error = ("" + $_)
    }

    # re-scan releases
    $latest = Get-ChildItem -Path $releasesDir -Filter "web_dashboard_v0__*.zip" -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
  }

  if (-not $latest) {
    $r = @{
      schema=$schema; ts_utc=(UtcIso)
      ok=$false; exit_code=2
      reason_code="INFRA.MISSING_RELEASE_ZIP"; child_reason_code="INFRA.MISSING_RELEASE_ZIP"
      repo=$repo; run_id=$runId
      error=("No web_dashboard_v0__*.zip in " + $releasesDir)
      observed=@{ build=$buildObserved }
    }
    EmitReportAndExit $r $outDir $evidenceDir
  }

  $ReleaseZip = $latest.FullName
}

if (-not (Test-Path $ReleaseZip)) {
  $r = @{
    schema=$schema; ts_utc=(UtcIso)
    ok=$false; exit_code=2
    reason_code="INFRA.RELEASE_ZIP_NOT_FOUND"; child_reason_code="INFRA.RELEASE_ZIP_NOT_FOUND"
    repo=$repo; run_id=$runId
    error=("release_zip_not_found: " + $ReleaseZip)
    observed=@{ build=$buildObserved }
  }
  EmitReportAndExit $r $outDir $evidenceDir
}

$releaseId = [IO.Path]::GetFileNameWithoutExtension($ReleaseZip)

# ---- install into CustomerBox ----
$box = Join-Path $CustomerBoxRoot ("SKU_" + $releaseId)
Remove-Item -Recurse -Force $box -ErrorAction SilentlyContinue
EnsureDir $box

Copy-Item -Force $ReleaseZip (Join-Path $box "release.zip")
Expand-Archive -Force -LiteralPath (Join-Path $box "release.zip") -DestinationPath $box

$exe = Join-Path $box "app.exe"
if (-not (Test-Path $exe)) {
  $r = @{
    schema=$schema; ts_utc=(UtcIso)
    ok=$false; exit_code=2
    reason_code="INFRA.EXE_NOT_FOUND_IN_RELEASE"; child_reason_code="INFRA.EXE_NOT_FOUND_IN_RELEASE"
    repo=$repo; run_id=$runId
    release_zip=$ReleaseZip
    box=$box
    observed=@{ build=$buildObserved }
  }
  EmitReportAndExit $r $outDir $evidenceDir
}

# ---- port handling ----
if ($Port -le 0) {
  $Port = Get-Random -Minimum 20000 -Maximum 40000
}

$stop = Join-Path $box "stop.flag"
if (Test-Path $stop) { Remove-Item -Force $stop }

# Kill old listener if any (best-effort)
try {
  $line = (netstat -ano | findstr (":{0}" -f $Port) | Select-Object -First 1)
  if ($line) {
    $pidOld = $line.ToString().Split()[-1]
    try { Stop-Process -Id $pidOld -Force } catch {}
    Start-Sleep -Milliseconds 300
  }
} catch {}

# Evidence: help
& $exe --help 1> (Join-Path $evidenceDir "help.json") 2> (Join-Path $evidenceDir "help.stderr.txt")

# Start server
$stdout = Join-Path $evidenceDir "serve.stdout.txt"
$stderr = Join-Path $evidenceDir "serve.stderr.txt"

$proc = Start-Process -FilePath $exe `
  -ArgumentList @("serve","--host","127.0.0.1","--port",$Port.ToString(),"--stop-flag","stop.flag","--ready-after-ms","200") `
  -WorkingDirectory $box -NoNewWindow -PassThru `
  -RedirectStandardOutput $stdout -RedirectStandardError $stderr

Start-Sleep -Milliseconds 200

# Poll /ready
$readyOk = $false
$readyLast = ""
for ($i=0; $i -lt 60; $i++) {
  $readyLast = (& curl.exe -sS ("http://127.0.0.1:{0}/ready" -f $Port) 2>&1)
  if ($readyLast -match '"ok"\s*:\s*true') { $readyOk = $true; break }
  Start-Sleep -Milliseconds 100
}
("READY_OK=" + $readyOk) | Out-File -Encoding utf8 (Join-Path $evidenceDir "ready_check.txt")
$readyLast | Out-File -Encoding utf8 (Join-Path $evidenceDir "ready_last.txt")

# Fetch endpoints
$rootPath   = Join-Path $evidenceDir "root.http.txt"
$uiPath     = Join-Path $evidenceDir "ui.http.txt"
$healthPath = Join-Path $evidenceDir "health.http.txt"
$readyPath  = Join-Path $evidenceDir "ready.http.txt"

CurlToFile ("http://127.0.0.1:{0}/" -f $Port)       $rootPath
CurlToFile ("http://127.0.0.1:{0}/ui" -f $Port)     $uiPath
CurlToFile ("http://127.0.0.1:{0}/health" -f $Port) $healthPath
CurlToFile ("http://127.0.0.1:{0}/ready" -f $Port)  $readyPath

$rootCode   = HttpCode $rootPath
$uiCode     = HttpCode $uiPath
$healthCode = HttpCode $healthPath
$readyCode  = HttpCode $readyPath

# Stop after checks
New-Item -ItemType File -Force -Path $stop | Out-Null
Start-Sleep -Seconds 1

$listening = $false
try {
  $ns = (netstat -ano | findstr (":{0}" -f $Port) | findstr "LISTENING" | Select-Object -First 1)
  if ($ns) { $listening = $true }
  netstat -ano | findstr (":{0}" -f $Port) | Out-File -Encoding utf8 (Join-Path $evidenceDir "netstat_after_stop.txt")
} catch {}

# ensure process exits
try {
  if (-not $proc.HasExited) { $proc.WaitForExit(5000) | Out-Null }
  if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
} catch {}

if (Test-Path $stop) { Remove-Item -Force $stop }

# ---- evaluate ----
$ok = $true
$reasonCode = "OK"
$childReason = "OK"

if (-not $readyOk) { $ok = $false; $reasonCode = "FAIL.READY_TIMEOUT"; $childReason = $reasonCode }
elseif ($rootCode -ne 200 -or $uiCode -ne 200 -or $healthCode -ne 200 -or $readyCode -ne 200) { $ok = $false; $reasonCode = "FAIL.HTTP_BAD"; $childReason = $reasonCode }
elseif ($listening) { $ok = $false; $reasonCode = "FAIL.STOP_FLAG_DID_NOT_STOP"; $childReason = $reasonCode }

$exitCode = 0
if (-not $ok) { $exitCode = 1 }

$report = @{
  schema=$schema
  ts_utc=(UtcIso)
  ok=$ok
  exit_code=$exitCode
  reason_code=$reasonCode
  child_reason_code=$childReason
  repo=$repo
  run_id=$runId
  release_zip=$ReleaseZip
  release_id=$releaseId
  port=$Port
  customer_box_root=$CustomerBoxRoot
  box=$box
  observed=@{
    build=$buildObserved
    ready_ok=$readyOk
    http_root=$rootCode
    http_ui=$uiCode
    http_health=$healthCode
    http_ready=$readyCode
    listening_after_stop=$listening
  }
}

EmitReportAndExit $report $outDir $evidenceDir
