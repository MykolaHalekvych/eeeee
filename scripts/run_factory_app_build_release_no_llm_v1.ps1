param(
  [Parameter(Mandatory=$true)][string]$KitId,
  [Parameter(Mandatory=$true)][string]$ProductId,
  [Parameter(Mandatory=$false)][string]$Summary = "",
  [Parameter(Mandatory=$false)][ValidateSet("YES","NO")][string]$RunAcceptance = "YES",
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exit codes (project standard)
$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')) }

function Ensure-Dir([string]$p) {
  if (-not (Test-Path -LiteralPath $p -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $p | Out-Null
  }
}

function Normalize-Exit([object]$v) {
  try { $n = [int]$v } catch { return $RC_INFRA }
  if ($n -eq 0 -or $n -eq 1 -or $n -eq 2) { return $n }
  return $RC_INFRA
}

function Tail-Line([string]$Raw, [int]$MaxLen = 400) {
  if ($null -eq $Raw) { return "" }
  $s = $Raw.Trim()
  if ($s -eq "") { return "" }
  $lines = $s -split "`r?`n"
  for ($i = $lines.Length - 1; $i -ge 0; $i--) {
    $c = $lines[$i].Trim()
    if ($c -ne "") {
      if ($c.Length -gt $MaxLen) { return $c.Substring($c.Length - $MaxLen) }
      return $c
    }
  }
  if ($s.Length -gt $MaxLen) { return $s.Substring($s.Length - $MaxLen) }
  return $s
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

function Safe-GetProp([object]$Obj, [string]$Name, [string]$Default="") {
  if ($null -eq $Obj) { return $Default }
  if ($Obj.PSObject.Properties.Match($Name).Count -gt 0) {
    $v = $Obj.$Name
    if ($null -eq $v) { return $Default }
    return [string]$v
  }
  return $Default
}

function Safe-GetBool([object]$Obj, [string]$Name, [bool]$Default=$false) {
  if ($null -eq $Obj) { return $Default }
  if ($Obj.PSObject.Properties.Match($Name).Count -gt 0) {
    try { return [bool]$Obj.$Name } catch { return $Default }
  }
  return $Default
}

function Write-Json([string]$Path, [object]$Obj) {
  Ensure-Dir (Split-Path -Parent $Path)
  ($Obj | ConvertTo-Json -Compress -Depth 50) | Set-Content -Encoding utf8 -Path $Path
}

function Write-Text([string]$Path, [string]$Text) {
  Ensure-Dir (Split-Path -Parent $Path)
  Set-Content -Encoding utf8 -Path $Path -Value $Text
}

function Emit-AndExit([hashtable]$Obj, [int]$Code) {
  $Obj.exit_code = [int]$Code
  $Obj.ok = ([int]$Code -eq 0)
  $Obj.ts_utc = UtcNowIso
  Write-Output ($Obj | ConvertTo-Json -Compress -Depth 50)
  exit ([int]$Code)
}

# ---------------- MAIN ----------------

# Defaults for catch (no unbound variables)
$repoPath = $Repo
$runId = ""

try {
  try { $repoPath = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { $repoPath = $Repo }

  $promptScript = Join-Path $repoPath "scripts\run_factory_app_prompt_v2.ps1"
  $buildScript  = Join-Path $repoPath "scripts\run_factory_app_build_release_v1.ps1"

  if (-not (Test-Path -LiteralPath $promptScript -PathType Leaf)) {
    Emit-AndExit ([ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="preflight_scripts"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=""
      error=@{ kind="infra"; type="missing_prompt_script"; message=("missing: {0}" -f $promptScript) }
    }) $RC_INFRA
  }

  if (-not (Test-Path -LiteralPath $buildScript -PathType Leaf)) {
    Emit-AndExit ([ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="preflight_scripts"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=""
      error=@{ kind="infra"; type="missing_build_script"; message=("missing: {0}" -f $buildScript) }
    }) $RC_INFRA
  }

  if ([string]::IsNullOrWhiteSpace($Summary)) { $Summary = "No-LLM BuildRelease v1" }

  # We'll fill these once we have run_id
  $runDir = ""
  $evidenceDir = ""
  $evStdout = ""
  $evStderr = ""

  # STEP 1) Prompt v2
  $promptRaw = ""
  try {
    $promptRaw = (& powershell -NoProfile -ExecutionPolicy Bypass -File $promptScript `
      -KitId $KitId -ProductId $ProductId -Summary $Summary 2>&1 | Out-String)
  } catch {
    $promptRaw = ($_ | Out-String)
  }

  $promptJson = Parse-OneJson $promptRaw
  if ($null -eq $promptJson) {
    Emit-AndExit ([ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="prompt_v2"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=""
      error=@{
        kind="infra"
        type="prompt_json_parse_failed"
        message="Prompt runner did not return a single JSON object."
        raw_tail=(Tail-Line $promptRaw)
      }
    }) $RC_INFRA
  }

  $promptExit = Normalize-Exit $promptJson.exit_code
  $runId = Safe-GetProp $promptJson "run_id" ""

  if ([string]::IsNullOrWhiteSpace($runId)) {
    Emit-AndExit ([ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="prompt_v2"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=""
      prompt=$promptJson
      error=@{ kind="infra"; type="missing_run_id"; message="Prompt JSON missing run_id." }
    }) $RC_INFRA
  }

  # Prepare run_dir evidence paths (best-effort)
  $runDir = Join-Path $repoPath ("args\data\runs\" + $runId)
  $evidenceDir = Join-Path $runDir "evidence"
  Ensure-Dir $evidenceDir

  $evStdout = Join-Path $evidenceDir "no_llm_build_release_v1.stdout.json"
  $evStderr = Join-Path $evidenceDir "no_llm_build_release_v1.stderr.txt"

  # Persist prompt raw/json as evidence
  try {
    Write-Text (Join-Path $evidenceDir "no_llm_prompt_v2.stdout_raw.txt") $promptRaw
    Write-Json (Join-Path $evidenceDir "no_llm_prompt_v2.stdout.json") $promptJson
  } catch {}

  if ($promptExit -ne 0) {
    $out = [ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="prompt_v2"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=$runId
      prompt=$promptJson
    }
    try { Write-Json $evStdout $out } catch {}
    Emit-AndExit $out $promptExit
  }

  # STEP 1.5) Preflight product manifest (before E2E/BuildRelease)
  $manifestPath = Join-Path $repoPath ("manifests\products\{0}.json" -f $ProductId)
  $pfStdout = Join-Path $evidenceDir "preflight_product_manifest.stdout.json"
  $pfStderr = Join-Path $evidenceDir "preflight_product_manifest.stderr.txt"

  if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    $out = [ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="preflight_product_manifest"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=$runId
      preflight=@{ ok=$false; manifest_path=$manifestPath }
      error=@{ kind="fail"; type="manifest_missing"; message=("product manifest not found: {0}" -f $manifestPath) }
    }
    try { Write-Text $pfStderr ($out.error.message + "`n") } catch {}
    try { Write-Json $pfStdout $out } catch {}
    try { Write-Json $evStdout $out } catch {}
    Emit-AndExit $out $RC_FAIL
  }

  $m = $null
  try {
    $m = (Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json -ErrorAction Stop)
  } catch {
    $msg = $_.Exception.Message
    $out = [ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="preflight_product_manifest"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=$runId
      preflight=@{ ok=$false; manifest_path=$manifestPath }
      error=@{ kind="fail"; type="manifest_parse_error"; message=$msg }
    }
    try { Write-Text $pfStderr ($msg + "`n") } catch {}
    try { Write-Json $pfStdout $out } catch {}
    try { Write-Json $evStdout $out } catch {}
    Emit-AndExit $out $RC_FAIL
  }

  $manifestProductId = Safe-GetProp $m "product_id" ""
  if ([string]$manifestProductId -ne [string]$ProductId) {
    $out = [ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="preflight_product_manifest"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=$runId
      preflight=@{ ok=$false; manifest_path=$manifestPath; manifest_product_id=$manifestProductId }
      error=@{
        kind="fail"
        type="manifest_product_id_mismatch"
        message=("manifest.product_id={0} expected={1}" -f $manifestProductId,$ProductId)
      }
    }
    try { Write-Json $pfStdout $out } catch {}
    try { Write-Json $evStdout $out } catch {}
    Emit-AndExit $out $RC_FAIL
  }

  # success evidence (optional but useful)
  try {
    Write-Json $pfStdout ([ordered]@{
      schema="preflight_product_manifest_v1"
      ok=$true
      product_id=$ProductId
      manifest_path=$manifestPath
    })
  } catch {}

  # STEP 2) No-LLM codegen copy
  $src = Join-Path $runDir "codegen_output.template.json"
  $dst = Join-Path $runDir "codegen_output.json"

  if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
    $out = [ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="codegen_copy"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=$runId
      copy=@{ ok=$false; src=$src; dst=$dst }
      error=@{ kind="fail"; type="missing_codegen_template"; message="Missing codegen_output.template.json" }
    }
    try { Write-Json $evStdout $out } catch {}
    Emit-AndExit $out $RC_FAIL
  }

  try {
    Copy-Item -Force $src $dst
  } catch {
    $msg = $_.Exception.Message
    $out = [ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="codegen_copy"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=$runId
      copy=@{ ok=$false; src=$src; dst=$dst }
      error=@{ kind="infra"; type="copy_failed"; message=$msg }
    }
    try { Write-Text $evStderr ($msg + "`n") } catch {}
    try { Write-Json $evStdout $out } catch {}
    Emit-AndExit $out $RC_INFRA
  }

  # STEP 3) BuildRelease v1
  $buildRaw = ""
  try {
    $buildRaw = (& powershell -NoProfile -ExecutionPolicy Bypass -File $buildScript `
      -RunId $runId -RunAcceptance $RunAcceptance 2>&1 | Out-String)
  } catch {
    $buildRaw = ($_ | Out-String)
  }

  $buildJson = Parse-OneJson $buildRaw
  if ($null -eq $buildJson) {
    $out = [ordered]@{
      schema="factory_build_release_no_llm_v1"
      step="build_release_v1"
      repo=$repoPath
      kit_id=$KitId
      product_id=$ProductId
      run_id=$runId
      copy=@{ ok=$true; src=$src; dst=$dst }
      error=@{
        kind="infra"
        type="buildrelease_json_parse_failed"
        message="BuildRelease runner did not return a single JSON object."
        raw_tail=(Tail-Line $buildRaw)
      }
    }
    try { Write-Text (Join-Path $evidenceDir "no_llm_build_release_v1.stdout_raw.txt") $buildRaw } catch {}
    try { Write-Json $evStdout $out } catch {}
    Emit-AndExit $out $RC_INFRA
  }

  $buildExit = Normalize-Exit $buildJson.exit_code

  # Persist build raw/json as evidence
  try {
    Write-Text (Join-Path $evidenceDir "no_llm_build_release_v1.stdout_raw.txt") $buildRaw
    Write-Json (Join-Path $evidenceDir "no_llm_build_release_v1.stdout.json") $buildJson
  } catch {}

  # SAFE extraction (no strict property access)
  $releaseId = Safe-GetProp $buildJson "release_id" ""
  $releaseZip = Safe-GetProp $buildJson "release_zip" ""
  $acceptanceOk = Safe-GetBool $buildJson "acceptance_ok" $false

  $out = [ordered]@{
    schema="factory_build_release_no_llm_v1"
    step="no_llm_build_release"
    repo=$repoPath
    kit_id=$KitId
    product_id=$ProductId
    run_id=$runId
    run_acceptance=$RunAcceptance
    preflight=@{ ok=$true; manifest_path=$manifestPath; manifest_product_id=$manifestProductId }
    prompt=@{
      schema=(Safe-GetProp $promptJson "schema" "")
      ok=(Safe-GetBool $promptJson "ok" $true)
      exit_code=(Normalize-Exit $promptJson.exit_code)
    }
    copy=@{ ok=$true; src=$src; dst=$dst }
    build_release=$buildJson
    release_id=$releaseId
    release_zip=$releaseZip
    acceptance_ok=$acceptanceOk
  }

  try { Write-Json $evStdout $out } catch {}
  Emit-AndExit $out $buildExit

} catch {
  $msg = $_.Exception.Message
  Emit-AndExit ([ordered]@{
    schema="factory_build_release_no_llm_v1"
    step="unhandled_exception"
    repo=$repoPath
    kit_id=$KitId
    product_id=$ProductId
    run_id=$runId
    error=@{ kind="infra"; type="exception"; message=$msg }
  }) $RC_INFRA
}
