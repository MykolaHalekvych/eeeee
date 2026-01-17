param(
  [string]$Repo = ".",
  [string]$RunId = "",
  [string]$OutDir = "",
  [string]$RunAcceptance = ""
)

$SCHEMA  = "eco_proof_l1_2sku_v1"
$RC_OK   = 0
$RC_FAIL = 1
$RC_INFRA= 2

function UtcTs() {
  return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

function Ensure-Dir([string]$DirPath) {
  if ($DirPath -and -not (Test-Path $DirPath)) {
    New-Item -ItemType Directory -Force -Path $DirPath | Out-Null
  }
}

function Write-Text([string]$Path, [string]$Text) {
  $p = Split-Path -Parent $Path
  Ensure-Dir $p
  $enc = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Path, ($Text -as [string]), $enc)
}

function Write-Json([string]$Path, $Obj) {
  $json = ($Obj | ConvertTo-Json -Depth 40 -Compress)
  Write-Text $Path $json
}

function Emit-JsonStdout($Obj) {
  $line = ($Obj | ConvertTo-Json -Depth 40 -Compress)
  [Console]::Out.WriteLine($line)
}

function New-RunId() {
  $ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
  $r  = -join ((1..8) | ForEach-Object { "{0:x}" -f (Get-Random -Max 16) })
  return "ECO_PROOF_L1_2SKU_{0}_{1}" -f $ts, $r
}

function Fail([string]$Reason, [string]$Child, [int]$Code, [string]$OutDir2) {
  $payload = @{
    schema = $SCHEMA
    ts_utc = (UtcTs)
    ok = $false
    exit_code = $Code
    reason_code = $Reason
    child_reason_code = $Child
    out_dir = $OutDir2
  }
  if ($OutDir2) { Write-Json (Join-Path $OutDir2 "final_report.json") $payload }
  Emit-JsonStdout $payload
  exit $Code
}

function Last-NonEmpty-Line([string]$Text) {
  $lines = $Text -split "(`r`n|`n|`r)"
  $non = @()
  foreach ($ln in $lines) {
    $t = ($ln + "").Trim()
    if ($t.Length -gt 0) { $non += $t }
  }
  if ($non.Count -gt 0) { return $non[$non.Count-1] }
  return ""
}

function Invoke-Child(
  [string]$StepId,
  [string]$ScriptPath,
  [string[]]$ScriptArgs,
  [int]$ExpectedRc,
  [string]$EvidenceDir
) {
  $stdoutPath = Join-Path $EvidenceDir ("{0}.stdout.txt" -f $StepId)
  $stderrPath = Join-Path $EvidenceDir ("{0}.stderr.txt" -f $StepId)

  $stdout = ""
  $rc = 999

  try {
    $stdout = & powershell -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @ScriptArgs 2> $stderrPath | Out-String
    $rc = $LASTEXITCODE
  } catch {
    $stdout = ""
    $rc = $RC_INFRA
    Write-Text $stderrPath ("EXCEPTION: " + $_.Exception.Message)
  }

  Write-Text $stdoutPath $stdout

  $jsonLine = Last-NonEmpty-Line $stdout
  $parseOk = $false
  $obj = $null
  $parseErr = ""
  if ($jsonLine.Length -gt 0) {
    try {
      $obj = $jsonLine | ConvertFrom-Json
      $parseOk = $true
    } catch {
      $parseOk = $false
      $parseErr = $_.Exception.Message
    }
  }

  $ok = $true
  if ($rc -ne $ExpectedRc) { $ok = $false }
  if (-not $parseOk) { $ok = $false }
  if ($parseOk) {
    if ($null -eq $obj.ok) { $ok = $false }
    if ([bool]$obj.ok -ne $true) { $ok = $false }
  }

  return @{
    step_id = $StepId
    ok = $ok
    expected = @{
      rc = $ExpectedRc
      script = $ScriptPath
      args = $ScriptArgs
    }
    observed = @{
      rc = $rc
      stdout_len = ($jsonLine.Length)
      parse_ok = $parseOk
      parse_error = $parseErr
      json = $obj
      stdout_path = $stdoutPath
      stderr_path = $stderrPath
    }
  }
}

# -------- main --------

$repoPath = (Resolve-Path $Repo).Path
if (-not $RunId) { $RunId = New-RunId }

if (-not $OutDir) {
  $OutDir = Join-Path $repoPath ("args\data\smoke\eco_proof_l1_2sku_v1\{0}" -f $RunId)
}
Ensure-Dir $OutDir

if ($RunAcceptance -ne "YES") {
  Fail "FAIL" "DENY_RUN_ACCEPTANCE_REQUIRED" $RC_FAIL $OutDir
}

$evidenceDir = Join-Path $OutDir "evidence"
Ensure-Dir $evidenceDir

$steps = @()

# SKU #1: web_dashboard_v0 server smoke
$steps += Invoke-Child `
  -StepId "01_web_dashboard_serve_smoke" `
  -ScriptPath (Join-Path $repoPath "scripts\web_dashboard_serve_smoke_v0.ps1") `
  -ScriptArgs @("-RunAcceptance","YES") `
  -ExpectedRc 0 `
  -EvidenceDir $evidenceDir

# SKU #2: cicd_release_pack_v0 build+release smoke
$steps += Invoke-Child `
  -StepId "02_cicd_build_release_smoke" `
  -ScriptPath (Join-Path $repoPath "scripts\demo_build_release_no_llm_smoke_v0.ps1") `
  -ScriptArgs @("-KitId","kit_cicd_release_pack_v0","-RunAcceptance","YES") `
  -ExpectedRc 0 `
  -EvidenceDir $evidenceDir

# SKU #2: cicd_release_pack_v0 CLI smoke
$steps += Invoke-Child `
  -StepId "03_cicd_cli_smoke" `
  -ScriptPath (Join-Path $repoPath "scripts\cicd_release_pack_cli_smoke_v0.ps1") `
  -ScriptArgs @("-RunAcceptance","YES") `
  -ExpectedRc 0 `
  -EvidenceDir $evidenceDir

$okAll = $true
$anyInfra = $false
foreach ($s in $steps) {
  if (-not [bool]$s.ok) { $okAll = $false }
  if ([int]$s.observed.rc -eq 2) { $anyInfra = $true }
}

$exitCode = $RC_OK
$reason = "OK"
$child = "OK"
if (-not $okAll) {
  $exitCode = $RC_FAIL
  if ($anyInfra) { $exitCode = $RC_INFRA }
  $reason = "FAIL"
  $child = "FAIL_STEP"
}

$payload = @{
  schema = $SCHEMA
  ts_utc = (UtcTs)
  ok = $okAll
  exit_code = $exitCode
  reason_code = $reason
  child_reason_code = $child
  run_id = $RunId
  repo = $repoPath
  out_dir = $OutDir
  evidence_dir = $evidenceDir
  steps = $steps
}

Write-Json (Join-Path $OutDir "final_report.json") $payload
Emit-JsonStdout $payload
exit $exitCode
