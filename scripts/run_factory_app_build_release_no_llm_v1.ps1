param(
  [Parameter(Mandatory=$true)][string]$KitId,
  [Parameter(Mandatory=$true)][string]$ProductId,
  [Parameter(Mandatory=$false)][string]$Summary = "",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso {
  return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
}

function Emit-AndExit([hashtable]$Obj, [int]$Code) {
  $Obj.exit_code = $Code
  $Obj.ok = ($Code -eq 0)
  $Obj.ts_utc = UtcNowIso
  $json = ($Obj | ConvertTo-Json -Compress)
  Write-Output $json
  exit $Code
}

function Normalize-Exit([object]$v) {
  try { $n = [int]$v } catch { return $RC_INFRA }
  if ($n -eq 0 -or $n -eq 1 -or $n -eq 2) { return $n }
  return $RC_INFRA
}

function Parse-OneJson([string]$Raw) {
  if ($null -eq $Raw) { return $null }
  $s = $Raw.Trim()
  if ($s -eq "") { return $null }

  try { return ($s | ConvertFrom-Json -ErrorAction Stop) } catch {}

  # Fallback: last line that looks like a JSON object
  $lines = $s -split "`r?`n"
  for ($i = $lines.Length - 1; $i -ge 0; $i--) {
    $c = $lines[$i].Trim()
    if ($c.StartsWith("{")) {
      try { return ($c | ConvertFrom-Json -ErrorAction Stop) } catch {}
    }
  }
  return $null
}

$repoPath = (Resolve-Path $Repo).Path
$promptScript = Join-Path $repoPath "scripts\run_factory_app_prompt_v2.ps1"
$buildScript  = Join-Path $repoPath "scripts\run_factory_app_build_release_v1.ps1"

if ([string]::IsNullOrWhiteSpace($Summary)) {
  $Summary = "No-LLM BuildRelease v1"
}

# 1) Prompt v2 (capture stdout+stderr; do not leak text)
$promptRaw = (& powershell -NoProfile -ExecutionPolicy Bypass -File $promptScript `
  -KitId $KitId -ProductId $ProductId -Summary $Summary 2>&1 | Out-String)

$promptJson = Parse-OneJson $promptRaw
if ($null -eq $promptJson) {
  Emit-AndExit ([ordered]@{
    schema = "factory_build_release_no_llm_v1"
    step = "prompt_v2"
    repo = $repoPath
    kit_id = $KitId
    product_id = $ProductId
    run_id = ""
    error = @{
      kind = "infra"
      type = "prompt_json_parse_failed"
      message = "Prompt runner did not return a single JSON object."
      raw_tail = ($promptRaw.Trim() | Select-Object -Last 1)
    }
  }) $RC_INFRA
}

$promptExit = Normalize-Exit $promptJson.exit_code
$runId = [string]$promptJson.run_id

if ([string]::IsNullOrWhiteSpace($runId)) {
  Emit-AndExit ([ordered]@{
    schema = "factory_build_release_no_llm_v1"
    step = "prompt_v2"
    repo = $repoPath
    kit_id = $KitId
    product_id = $ProductId
    run_id = ""
    prompt = $promptJson
    error = @{
      kind = "infra"
      type = "missing_run_id"
      message = "Prompt JSON missing run_id."
    }
  }) $RC_INFRA
}

# Prepare evidence paths (best-effort; never print extra)
$runDir = Join-Path $repoPath ("args\data\runs\" + $runId)
$evidenceDir = Join-Path $runDir "evidence"
$evStdout = Join-Path $evidenceDir "no_llm_build_release_v1.stdout.json"
$evStderr = Join-Path $evidenceDir "no_llm_build_release_v1.stderr.txt"

try { New-Item -ItemType Directory -Force $evidenceDir | Out-Null } catch {}

if ($promptExit -ne 0) {
  $out = [ordered]@{
    schema = "factory_build_release_no_llm_v1"
    step = "prompt_v2"
    repo = $repoPath
    kit_id = $KitId
    product_id = $ProductId
    run_id = $runId
    prompt = $promptJson
  }
  try { ($out | ConvertTo-Json -Compress) | Set-Content -Encoding utf8 $evStdout } catch {}
  Emit-AndExit $out $promptExit
}

# 2) No-LLM codegen copy
$src = Join-Path $runDir "codegen_output.template.json"
$dst = Join-Path $runDir "codegen_output.json"

if (-not (Test-Path -LiteralPath $src)) {
  $out = [ordered]@{
    schema = "factory_build_release_no_llm_v1"
    step = "codegen_copy"
    repo = $repoPath
    kit_id = $KitId
    product_id = $ProductId
    run_id = $runId
    prompt = $promptJson
    copy = @{
      ok = $false
      src = $src
      dst = $dst
    }
    error = @{
      kind = "fail"
      type = "missing_codegen_template"
      message = "Missing codegen_output.template.json"
    }
  }
  try { ($out | ConvertTo-Json -Compress) | Set-Content -Encoding utf8 $evStdout } catch {}
  Emit-AndExit $out $RC_FAIL
}

try {
  Copy-Item -Force $src $dst
} catch {
  $out = [ordered]@{
    schema = "factory_build_release_no_llm_v1"
    step = "codegen_copy"
    repo = $repoPath
    kit_id = $KitId
    product_id = $ProductId
    run_id = $runId
    prompt = $promptJson
    copy = @{
      ok = $false
      src = $src
      dst = $dst
    }
    error = @{
      kind = "infra"
      type = "copy_failed"
      message = $_.Exception.Message
    }
  }
  try { $_ | Out-String | Set-Content -Encoding utf8 $evStderr } catch {}
  try { ($out | ConvertTo-Json -Compress) | Set-Content -Encoding utf8 $evStdout } catch {}
  Emit-AndExit $out $RC_INFRA
}

# 3) BuildRelease v1
$buildRaw = (& powershell -NoProfile -ExecutionPolicy Bypass -File $buildScript `
  -RunId $runId -RunAcceptance $RunAcceptance 2>&1 | Out-String)

$buildJson = Parse-OneJson $buildRaw
if ($null -eq $buildJson) {
  $out = [ordered]@{
    schema = "factory_build_release_no_llm_v1"
    step = "build_release_v1"
    repo = $repoPath
    kit_id = $KitId
    product_id = $ProductId
    run_id = $runId
    prompt = $promptJson
    copy = @{ ok = $true; src = $src; dst = $dst }
    error = @{
      kind = "infra"
      type = "buildrelease_json_parse_failed"
      message = "BuildRelease runner did not return a single JSON object."
      raw_tail = ($buildRaw.Trim() | Select-Object -Last 1)
    }
  }
  try { ($out | ConvertTo-Json -Compress) | Set-Content -Encoding utf8 $evStdout } catch {}
  Emit-AndExit $out $RC_INFRA
}

$buildExit = Normalize-Exit $buildJson.exit_code

$out = [ordered]@{
  schema = "factory_build_release_no_llm_v1"
  step = "no_llm_build_release"
  repo = $repoPath
  kit_id = $KitId
  product_id = $ProductId
  run_id = $runId
  run_acceptance = $RunAcceptance
  prompt = @{
    schema = $promptJson.schema
    ok = $promptJson.ok
    exit_code = $promptJson.exit_code
  }
  copy = @{ ok = $true; src = $src; dst = $dst }
  build_release = $buildJson
  release_id = $buildJson.release_id
  release_zip = $buildJson.release_zip
  acceptance_ok = $buildJson.acceptance_ok
}

try { ($out | ConvertTo-Json -Compress) | Set-Content -Encoding utf8 $evStdout } catch {}

Emit-AndExit $out $buildExit
