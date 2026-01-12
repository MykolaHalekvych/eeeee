param(
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Exit codes (project standard)
$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")) }
function UtcNowId  { return ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")) }
function RandHex([int]$n) {
  $rng = New-Object System.Random
  $chars = "0123456789abcdef".ToCharArray()
  $sb = New-Object System.Text.StringBuilder
  for ($i=0; $i -lt $n; $i++) { [void]$sb.Append($chars[$rng.Next(0,16)]) }
  return $sb.ToString()
}

function WriteUtf8NoBom([string]$Path, [string]$Text) {
  $enc = New-Object System.Text.UTF8Encoding $false
  $dir = Split-Path -Parent $Path
  if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
  [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Emit-And-Exit([hashtable]$Obj, [int]$Code) {
  $Obj.exit_code = $Code
  $Obj.ok = ($Code -eq 0)
  $json = ($Obj | ConvertTo-Json -Depth 20 -Compress)
  try { if ($Obj.out_dir) { WriteUtf8NoBom -Path (Join-Path $Obj.out_dir "summary.json") -Text $json } } catch {}
  Write-Output $json
  exit $Code
}

function Normalize-ExitCode([int]$Code) {
  if ($Code -eq 0) { return 0 }
  if ($Code -eq 1) { return 1 }
  if ($Code -eq 2) { return 2 }
  return 2
}

function Run-Step {
  param(
    [Parameter(Mandatory=$true)][string]$RepoRoot,
    [Parameter(Mandatory=$true)][string]$OutDir,
    [Parameter(Mandatory=$true)][int]$Index,
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][string]$ScriptRel
  )

  $stepDirName = ("{0:D2}_{1}" -f $Index, $Name)
  $stepDir = Join-Path $OutDir (Join-Path "steps" $stepDirName)
  New-Item -ItemType Directory -Force -Path $stepDir | Out-Null

  $scriptPath = Join-Path $RepoRoot $ScriptRel
  $stdoutPath = Join-Path $stepDir "stdout.json"
  $stderrPath = Join-Path $stepDir "stderr.txt"

  $started = [DateTime]::UtcNow
  $rc = 2
  $stdout = ""
  $stderr = ""

  if (-not (Test-Path $scriptPath)) {
    $stderr = "SCRIPT_NOT_FOUND: $scriptPath"
    WriteUtf8NoBom -Path $stdoutPath -Text '{"schema":"child_runner","ok":false,"exit_code":2,"error":{"kind":"SCRIPT_NOT_FOUND"}}'
    WriteUtf8NoBom -Path $stderrPath -Text $stderr
    return @{
      name = $Name
      script = $ScriptRel
      script_path = $scriptPath
      status = "INFRA"
      exit_code = 2
      duration_ms = 0
      stdout_path = $stdoutPath
      stderr_path = $stderrPath
      child_json_ok = $false
    }
  }

  try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $scriptPath + '"'
    $psi.WorkingDirectory = $RepoRoot
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true

    # Best-effort: force UTF-8 decoding of child streams (PS 5.1 on .NET Framework supports this)
    try { $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
    try { $psi.StandardErrorEncoding  = [System.Text.Encoding]::UTF8 } catch {}

    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi

    $null = $p.Start()
    $stdout = $p.StandardOutput.ReadToEnd()
    $stderr = $p.StandardError.ReadToEnd()
    $p.WaitForExit()

    $rc = Normalize-ExitCode -Code $p.ExitCode
  }
  catch {
    $rc = 2
    $stderr = ("EXCEPTION: {0}" -f $_.Exception.Message)
  }

  $ended = [DateTime]::UtcNow
  $durMs = [int]([TimeSpan]($ended - $started)).TotalMilliseconds

  if ([string]::IsNullOrWhiteSpace($stdout)) { $stdout = "" }
  if ([string]::IsNullOrWhiteSpace($stderr)) { $stderr = "" }

  # Persist child output as UTF-8 no BOM
  WriteUtf8NoBom -Path $stdoutPath -Text $stdout
  WriteUtf8NoBom -Path $stderrPath -Text $stderr

  $status = "INFRA"
  if ($rc -eq 0) { $status = "PASS" }
  elseif ($rc -eq 1) { $status = "FAIL" }
  else { $status = "INFRA" }

  $childJsonOk = $false
  try {
    if (-not [string]::IsNullOrWhiteSpace($stdout)) {
      $null = ($stdout | ConvertFrom-Json)
      $childJsonOk = $true
    }
  } catch { $childJsonOk = $false }

  return @{
    name = $Name
    script = $ScriptRel
    script_path = $scriptPath
    status = $status
    exit_code = $rc
    duration_ms = $durMs
    stdout_path = $stdoutPath
    stderr_path = $stderrPath
    child_json_ok = $childJsonOk
  }
}

try {
  $repoRoot = (Resolve-Path $Repo).Path
  Set-Location $repoRoot

  # RepoGuard
  if (-not (Test-Path (Join-Path $repoRoot ".args_engine_repo"))) { throw "WRONG_REPO" }

  $runId = ("ENGINE_CI_{0}_{1}" -f (UtcNowId), (RandHex 8))
  $outDir = Join-Path $repoRoot (Join-Path "args\data\smoke\engine_ci_suite_v0" $runId)
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null

  $stepsSpec = @(
    @{ name="family_chain_smoke_v0";              script="scripts\demo_family_chain_smoke_v0.ps1" },
    @{ name="family_chain_negative_no_stage_v0";  script="scripts\demo_family_chain_negative_no_stage_v0.ps1" },
    @{ name="family_chain_negative_corrupt_zip_v0"; script="scripts\demo_family_chain_negative_corrupt_zip_v0.ps1" },
    @{ name="family_chain_zip_discovery_v0";      script="scripts\demo_family_chain_zip_discovery_v0.ps1" },
    @{ name="kit_registry_smoke_v0";              script="scripts\demo_kit_registry_smoke_v0.ps1" },
    @{ name="template_pack_matrix_v0";            script="scripts\demo_template_pack_matrix_v0.ps1" }
  )

  $steps = @()
  $worst = 0  # 0 OK, 1 FAIL, 2 INFRA

  for ($i=0; $i -lt $stepsSpec.Count; $i++) {
    $s = $stepsSpec[$i]
    $res = Run-Step -RepoRoot $repoRoot -OutDir $outDir -Index ($i+1) -Name $s.name -ScriptRel $s.script
    $steps += $res
    if ($res.exit_code -gt $worst) { $worst = $res.exit_code }
  }

  $final = @{
    schema = "demo_engine_ci_suite_v0"
    ts_utc = (UtcNowIso)
    repo = $repoRoot
    run_id = $runId
    out_dir = $outDir
    status = (if ($worst -eq 0) { "PASS" } elseif ($worst -eq 1) { "FAIL" } else { "INFRA" })
    steps = $steps
  }

  Emit-And-Exit -Obj $final -Code $worst
}
catch {
  $final = @{
    schema = "demo_engine_ci_suite_v0"
    ts_utc = (UtcNowIso)
    repo = $Repo
    run_id = ""
    out_dir = ""
    status = "INFRA"
    error = @{
      kind = "exception"
      message = $_.Exception.Message
    }
    steps = @()
  }
  Emit-And-Exit -Obj $final -Code $RC_INFRA
}
