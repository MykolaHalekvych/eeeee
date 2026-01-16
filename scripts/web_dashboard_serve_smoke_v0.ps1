param(
  [string]$ReleaseZip = "",
  [string]$CustomerBoxRoot = "C:\Customer Box\тест",
  [int]$Port = 17812,
  [string]$RunAcceptance = "YES"
)

$ErrorActionPreference = "Stop"

function UtcId { return [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") }
function RandHex8 { return -join (1..8 | ForEach-Object { "{0:x}" -f (Get-Random -Max 16) }) }

function WriteJson([string]$path, $obj) {
  ($obj | ConvertTo-Json -Depth 10 -Compress) | Out-File -Encoding utf8 $path
}

function CurlToFile([string]$url, [string]$path) {
  & curl.exe -sS -i $url 2>&1 | Out-File -Encoding utf8 $path
}

function HttpCode([string]$path) {
  $line = (Get-Content $path -TotalCount 1)
  if ($line -match "HTTP/\S+\s+(\d+)") { return [int]$matches[1] }
  return 0
}

try {
  $repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

  if ($RunAcceptance -ne "YES") {
    $r = @{
      schema="web_dashboard_serve_smoke_v0"; ts_utc=[DateTime]::UtcNow.ToString("o").Replace("+00:00","Z")
      ok=$false; exit_code=1; repo=$repo; reason="RUN_ACCEPTANCE_REQUIRED"
    }
    Write-Output ($r | ConvertTo-Json -Compress)
    exit 1
  }

  if (-not $ReleaseZip) {
    $latest = Get-ChildItem -Path (Join-Path $repo "dist\releases") -Filter "web_dashboard_v0__*.zip" |
      Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if (-not $latest) { throw "missing_release_zip" }
    $ReleaseZip = $latest.FullName
  }
  if (-not (Test-Path $ReleaseZip)) { throw ("release_zip_not_found:" + $ReleaseZip) }

  $releaseId = [IO.Path]::GetFileNameWithoutExtension($ReleaseZip)
  $runId = "WEB_DASHBOARD_SERVE_SMOKE_" + (UtcId) + "_" + (RandHex8)

  $outDir = Join-Path $repo (Join-Path "args\data\smoke\web_dashboard_serve_smoke_v0" $runId)
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null

  # Install into CustomerBox (spaces + unicode)
  New-Item -ItemType Directory -Force -Path $CustomerBoxRoot | Out-Null
  $box = Join-Path $CustomerBoxRoot ("SKU_" + $releaseId)
  Remove-Item -Recurse -Force $box -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $box | Out-Null

  Copy-Item -Force $ReleaseZip (Join-Path $box "release.zip")
  Expand-Archive -Force -LiteralPath (Join-Path $box "release.zip") -DestinationPath $box

  $exe = Join-Path $box "app.exe"
  if (-not (Test-Path $exe)) { throw "exe_not_found_in_box" }

  $stop = Join-Path $box "stop.flag"
  if (Test-Path $stop) { Remove-Item -Force $stop }

  # Kill old listener if any
  $line = (netstat -ano | findstr (":{0}" -f $Port) | Select-Object -First 1)
  if ($line) {
    $pidOld = $line.ToString().Split()[-1]
    try { Stop-Process -Id $pidOld -Force } catch {}
    Start-Sleep -Milliseconds 300
  }

  # Evidence: help
  & $exe --help 1> (Join-Path $outDir "help.json") 2> (Join-Path $outDir "help.stderr.txt")

  # Start server
  $stdout = Join-Path $outDir "serve.stdout.txt"
  $stderr = Join-Path $outDir "serve.stderr.txt"

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
    if ($readyLast -match '"ok":true') { $readyOk = $true; break }
    Start-Sleep -Milliseconds 100
  }
  ("READY_OK=" + $readyOk) | Out-File -Encoding utf8 (Join-Path $outDir "ready_check.txt")
  $readyLast | Out-File -Encoding utf8 (Join-Path $outDir "ready_last.txt")

  # Fetch endpoints
  $rootPath   = Join-Path $outDir "root.http.txt"
  $uiPath     = Join-Path $outDir "ui.http.txt"
  $healthPath = Join-Path $outDir "health.http.txt"
  $readyPath  = Join-Path $outDir "ready.http.txt"

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
  $ns = (netstat -ano | findstr (":{0}" -f $Port) | findstr "LISTENING" | Select-Object -First 1)
  if ($ns) { $listening = $true }
  netstat -ano | findstr (":{0}" -f $Port) | Out-File -Encoding utf8 (Join-Path $outDir "netstat_after_stop.txt")

  if (!$proc.HasExited) { $proc.WaitForExit(5000) | Out-Null }
  if (Test-Path $stop) { Remove-Item -Force $stop }

  $ok = $true
  if (-not $readyOk) { $ok = $false }
  if ($rootCode -ne 200) { $ok = $false }
  if ($uiCode -ne 200) { $ok = $false }
  if ($healthCode -ne 200) { $ok = $false }
  if ($readyCode -ne 200) { $ok = $false }
  if ($listening) { $ok = $false }

  $exitCode = 0
  $reason = "OK"
  if (-not $ok) { $exitCode = 1; $reason = "FAIL" }

  $report = @{
    schema="web_dashboard_serve_smoke_v0"
    ts_utc=[DateTime]::UtcNow.ToString("o").Replace("+00:00","Z")
    ok=$ok
    exit_code=$exitCode
    repo=$repo
    run_id=$runId
    out_dir=$outDir
    release_zip=$ReleaseZip
    port=$Port
    box=$box
    observed=@{
      ready_ok=$readyOk
      http_root=$rootCode
      http_ui=$uiCode
      http_health=$healthCode
      http_ready=$readyCode
      listening_after_stop=$listening
    }
    reason=$reason
  }

  WriteJson (Join-Path $outDir "final_report.json") $report
  Write-Output ($report | ConvertTo-Json -Compress)
  exit $exitCode
}
catch {
  $repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
  $outDir2 = Join-Path $repo (Join-Path "args\data\smoke\web_dashboard_serve_smoke_v0" ("INFRA_" + (UtcId) + "_" + (RandHex8)))
  New-Item -ItemType Directory -Force -Path $outDir2 | Out-Null
  ("" + $_) | Out-File -Encoding utf8 (Join-Path $outDir2 "error.txt")

  $report = @{
    schema="web_dashboard_serve_smoke_v0"
    ts_utc=[DateTime]::UtcNow.ToString("o").Replace("+00:00","Z")
    ok=$false
    exit_code=2
    repo=$repo
    run_id="INFRA"
    out_dir=$outDir2
    reason="INFRA"
    error=(""+$_)
  }
  WriteJson (Join-Path $outDir2 "final_report.json") $report
  Write-Output ($report | ConvertTo-Json -Compress)
  exit 2
}
