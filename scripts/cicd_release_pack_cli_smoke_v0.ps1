param(
  [string]$Repo = ".",
  [string]$ExePath = "",
  [string]$OutDir = "",
  [string]$RunId = "",
  [string]$RunAcceptance = ""
)

$SCHEMA  = "cicd_release_pack_cli_smoke_v0"
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
  $enc = New-Object System.Text.UTF8Encoding($false) # no BOM
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
  return "CICD_CLI_SMOKE_{0}_{1}" -f $ts, $r
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
  if ($OutDir2) {
    Write-Json (Join-Path $OutDir2 "final_report.json") $payload
  }
  Emit-JsonStdout $payload
  exit $Code
}

function Invoke-ExeStep(
  [string]$StepId,
  [string[]]$CliArgs,
  [int]$ExpectedRc,
  [bool]$ExpectedOk,
  [string]$ExpectedCmd,
  [string]$ExpectedReasonCode,
  [string]$ExpectedStdoutMode,
  [string]$Exe,
  [string]$EvidenceDir
) {
  $stdoutPath = Join-Path $EvidenceDir ("{0}.stdout.txt" -f $StepId)
  $stderrPath = Join-Path $EvidenceDir ("{0}.stderr.txt" -f $StepId)

  $stdout = ""
  $rc = 999
  try {
    # IMPORTANT: @CliArgs (never @Args / $Args)
    $stdout = & $Exe @CliArgs 2> $stderrPath | Out-String
    $rc = $LASTEXITCODE
  } catch {
    $stdout = ""
    $rc = $RC_INFRA
    Write-Text $stderrPath ("EXCEPTION: " + $_.Exception.Message)
  }

  Write-Text $stdoutPath $stdout

  $stdoutTrim = ($stdout | Out-String).Trim()
  $parseOk = $false
  $obj = $null
  $parseErr = ""

  if ($stdoutTrim.Length -gt 0) {
    try {
      $obj = $stdoutTrim | ConvertFrom-Json
      $parseOk = $true
    } catch {
      $parseOk = $false
      $parseErr = $_.Exception.Message
    }
  }

  $ok = $true

  # Basic gates
  if ($rc -ne $ExpectedRc) { $ok = $false }
  if ($stdoutTrim.Length -le 0) { $ok = $false }
  if (-not $parseOk) { $ok = $false }

  # Contract gates
  if ($parseOk) {
    if ($obj.schema -ne "cicd_release_pack_v0_cli_v1") { $ok = $false }
    if ($obj.product_id -ne "cicd_release_pack_v0") { $ok = $false }
    if ([int]$obj.exit_code -ne $ExpectedRc) { $ok = $false }
    if ([bool]$obj.ok -ne $ExpectedOk) { $ok = $false }

    if ($ExpectedCmd -and ($obj.cmd -ne $ExpectedCmd)) { $ok = $false }
    if ($ExpectedReasonCode -and ($obj.reason_code -ne $ExpectedReasonCode)) { $ok = $false }

    if (-not $obj.child_reason_code) { $ok = $false }
    if (-not $obj.ts_utc) { $ok = $false }

    # EXE-only proof (guard against accidentally calling python)
    if ($null -eq $obj.is_frozen) { $ok = $false }
    if ([bool]$obj.is_frozen -ne $true) { $ok = $false }

    # Stdout mode proof
    if ($ExpectedStdoutMode -and ($obj.stdout_mode -ne $ExpectedStdoutMode)) { $ok = $false }
  }

  return @{
    step_id = $StepId
    ok = $ok
    expected = @{
      cli_args = $CliArgs
      rc = $ExpectedRc
      ok = $ExpectedOk
      cmd = $ExpectedCmd
      reason_code = $ExpectedReasonCode
      stdout_mode = $ExpectedStdoutMode
      schema = "cicd_release_pack_v0_cli_v1"
      product_id = "cicd_release_pack_v0"
    }
    observed = @{
      rc = $rc
      stdout_len = $stdoutTrim.Length
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

if (-not $ExePath) {
  $ExePath = Join-Path $repoPath "dist\cicd_release_pack_v0\app.exe"
} else {
  $ExePath = (Resolve-Path $ExePath).Path
}

if (-not $OutDir) {
  $OutDir = Join-Path $repoPath ("args\data\smoke\cicd_release_pack_cli_smoke_v0\{0}" -f $RunId)
}
Ensure-Dir $OutDir

if ($RunAcceptance -ne "YES") {
  Fail "FAIL" "DENY_RUN_ACCEPTANCE_REQUIRED" $RC_FAIL $OutDir
}

if (-not (Test-Path $ExePath)) {
  Fail "FAIL" "INFRA_EXE_NOT_FOUND" $RC_INFRA $OutDir
}

$evidenceDir = Join-Path $OutDir "evidence"
Ensure-Dir $evidenceDir

# Expected stdout mode (we proved this in EXE output)
$expectedStdoutMode = "win_writefile_or_oswrite_v2"

$steps = @()
$steps += Invoke-ExeStep -StepId "01_help"     -CliArgs @("--help")         -ExpectedRc 0 -ExpectedOk $true  -ExpectedCmd "help"     -ExpectedReasonCode "OK"                  -ExpectedStdoutMode $expectedStdoutMode -Exe $ExePath -EvidenceDir $evidenceDir
$steps += Invoke-ExeStep -StepId "02_version"  -CliArgs @("version")        -ExpectedRc 0 -ExpectedOk $true  -ExpectedCmd "version"  -ExpectedReasonCode "OK"                  -ExpectedStdoutMode $expectedStdoutMode -Exe $ExePath -EvidenceDir $evidenceDir
$steps += Invoke-ExeStep -StepId "03_selftest" -CliArgs @("selftest")       -ExpectedRc 0 -ExpectedOk $true  -ExpectedCmd "selftest" -ExpectedReasonCode "OK"                  -ExpectedStdoutMode $expectedStdoutMode -Exe $ExePath -EvidenceDir $evidenceDir
$steps += Invoke-ExeStep -StepId "04_ping"     -CliArgs @("ping")           -ExpectedRc 0 -ExpectedOk $true  -ExpectedCmd "ping"     -ExpectedReasonCode "OK"                  -ExpectedStdoutMode $expectedStdoutMode -Exe $ExePath -EvidenceDir $evidenceDir
$steps += Invoke-ExeStep -StepId "05_bogus"    -CliArgs @("bogus_command")  -ExpectedRc 1 -ExpectedOk $false -ExpectedCmd "unknown"  -ExpectedReasonCode "FAIL_UNKNOWN_COMMAND" -ExpectedStdoutMode $expectedStdoutMode -Exe $ExePath -EvidenceDir $evidenceDir

$okAll = $true
foreach ($s in $steps) {
  if (-not [bool]$s.ok) { $okAll = $false }
}

$exitCode = $RC_OK
$reason = "OK"
$child = "OK"
if (-not $okAll) {
  $exitCode = $RC_FAIL
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
  exe_path = $ExePath
  out_dir = $OutDir
  evidence_dir = $evidenceDir
  steps = $steps
}

Write-Json (Join-Path $OutDir "final_report.json") $payload
Emit-JsonStdout $payload
exit $exitCode
