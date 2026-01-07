param(
  [Parameter(Mandatory=$true)][string]$KitId,
  [Parameter(Mandatory=$false)][string]$ProductId = "",
  [Parameter(Mandatory=$false)][string]$Summary = "",
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Exit codes (project standard)
$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowId  { return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')) }
function UtcNowIso { return ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')) }

function CoalesceStr([object]$v) {
  if ($null -eq $v) { return "" }
  return [string]$v
}

# ONE canonical emitter:
# - always one JSON in stdout
# - always Flush
# - always exits with Code
function Emit-AndExit {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory = $true)][AllowNull()][object]$Payload,
    [Parameter(Mandatory = $true)][int]$Code
  )

  if ($null -eq $Payload) {
    $Payload = [ordered]@{
      schema = 'factory_prompt_v2'
      error  = [ordered]@{ kind='null_payload'; message='Emit-AndExit called with null payload.' }
    }
    $Code = $RC_INFRA
  }

  $obj = $null
  if ($Payload -is [System.Collections.IDictionary]) {
    $obj = $Payload
  } else {
    $obj = [ordered]@{}
    try {
      $props = $Payload | Get-Member -MemberType NoteProperty,Property | Select-Object -ExpandProperty Name
      foreach ($n in $props) { $obj[$n] = $Payload.$n }
      if ($obj.Count -eq 0) { $obj['payload'] = [string]$Payload }
    } catch {
      $obj = [ordered]@{ payload = [string]$Payload }
    }
  }

  $obj['exit_code'] = $Code
  $obj['ok']        = ($Code -eq $RC_OK)

  $json = $null
  try {
    $json = ConvertTo-Json -InputObject $obj -Depth 50 -Compress
  } catch {
    $fallback = [ordered]@{
      schema    = 'factory_prompt_v2'
      ok        = $false
      exit_code = $RC_INFRA
      error     = [ordered]@{
        kind      = 'json_serialize_failed'
        message   = 'ConvertTo-Json failed in Emit-AndExit.'
        exception = $_.Exception.Message
      }
    }
    $json = ConvertTo-Json -InputObject $fallback -Depth 20 -Compress
    $Code = $RC_INFRA
  }

  if ([string]::IsNullOrWhiteSpace($json)) {
    $json = '{"schema":"factory_prompt_v2","ok":false,"exit_code":2,"error":{"kind":"empty_stdout","message":"Emit-AndExit produced an empty JSON string."}}'
    $Code = $RC_INFRA
  }

  Write-Output $json
  [Console]::Out.Flush()
  exit $Code
}

function Ensure-Dir([string]$Path) {
  if (-not (Test-Path -Path $Path -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
  }
}

function Safe-ReadJson([string]$Path) {
  try {
    $raw = Get-Content -Raw -Encoding UTF8 -Path $Path -ErrorAction Stop
    if ($null -eq $raw -or $raw.Length -eq 0) { throw "Empty file" }
    $raw = $raw -replace "^\uFEFF",""   # BOM-safe
    return ($raw | ConvertFrom-Json)
  } catch {
    throw "JSON parse failed: $Path ($($_.Exception.Message))"
  }
}

# Step logger (never touches stdout)
$script:StepsLog = $null
function Init-StepsLog([string]$EvidenceDir) {
  try {
    $script:StepsLog = Join-Path $EvidenceDir "prompt_steps.log"
    Add-Content -Encoding utf8 -Path $script:StepsLog -Value ("{0}`tINIT" -f (UtcNowIso)) 2>$null
  } catch { }
}
function Trace-Step([string]$Msg) {
  if ([string]::IsNullOrWhiteSpace($script:StepsLog)) { return }
  try {
    Add-Content -Encoding utf8 -Path $script:StepsLog -Value ("{0}`t{1}" -f (UtcNowIso), $Msg) 2>$null
  } catch { }
}

# Read template pack file content with size caps (prevents ConvertTo-Json stalls)
function Read-TemplatePackContent([string]$RepoRoot, [string]$KitId, [string]$RelPath) {
  $packRoot = Join-Path $RepoRoot ("manifests\template_packs\" + $KitId)
  $src = Join-Path $packRoot $RelPath

  if (-not (Test-Path -Path $src -PathType Leaf)) { return "" }

  # per-file size cap
  $maxBytes = 262144 # 256 KB
  try {
    $fi = Get-Item -Path $src -ErrorAction Stop
    if ($fi.Length -gt $maxBytes) { return "" }
  } catch {
    return ""
  }

  try {
    $c = (Get-Content -Raw -Encoding UTF8 -Path $src -ErrorAction Stop)
    if ($null -eq $c) { return "" }
    return ($c -replace "^\uFEFF","") # BOM-safe
  } catch {
    return ""
  }
}

# Python runner: NO Start-Process (so args with spaces stay intact)
# Writes stdout in UTF-8 explicitly (Set-Content), and keeps stderr evidence.
function Invoke-PyModule {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory=$true)][string[]]$Args,
    [Parameter(Mandatory=$true)][string]$StdoutPath,
    [Parameter(Mandatory=$true)][string]$StderrPath,
    [Parameter(Mandatory=$false)][string]$StepName = ""
  )

  Remove-Item -Force -ErrorAction SilentlyContinue -Path $StdoutPath,$StderrPath

  $py = $null
  try { $py = (Get-Command py -ErrorAction Stop).Source } catch {
    try { Set-Content -Encoding utf8 -Path $StderrPath -Value "py launcher not found." } catch { }
    "" | Set-Content -Encoding utf8 -Path $StdoutPath
    return [ordered]@{ rc = $RC_INFRA }
  }

  $safeArgs = @()
  foreach ($a in $Args) { if ($null -ne $a) { $safeArgs += ([string]$a) } }

  if ($StepName) { Trace-Step ("BEFORE {0}" -f $StepName) }
  Trace-Step ("PY_ARGS: " + ($safeArgs -join " | "))

  $rc = $RC_INFRA
  $stdoutText = ""
  try {
    # stdout captured as lines -> then persisted as UTF-8 (avoids PS redirection UTF-16 traps)
    $lines = & $py @safeArgs 2> $StderrPath
    $rc = [int]$LASTEXITCODE
    if ($null -ne $lines) { $stdoutText = ($lines -join "`n") }
  } catch {
    $rc = $RC_INFRA
    $stdoutText = ""
    try { Set-Content -Encoding utf8 -Path $StderrPath -Value $_.Exception.Message } catch { }
  }

  $stdoutText | Set-Content -Encoding utf8 -Path $StdoutPath

  if ($StepName) { Trace-Step ("AFTER  {0} rc={1}" -f $StepName, $rc) }
  return [ordered]@{ rc = [int]$rc }
}

# =========================
# MAIN
# =========================
$repoResolved = $null
try { $repoResolved = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { $repoResolved = $Repo }

try {
  $ts_utc = UtcNowIso

  if (-not (Test-Path -Path $repoResolved -PathType Container)) {
    Emit-AndExit ([ordered]@{
      schema='factory_prompt_v2'; ts_utc=$ts_utc; repo=$repoResolved; step='preflight'
      error=("Repo path not found: {0}" -f $repoResolved)
    }) $RC_INFRA
  }

  # IMPORTANT: run from Repo as working dir (module resolution + relative paths)
  Push-Location -Path $repoResolved

  $run_id   = (UtcNowId) + '_' + ([guid]::NewGuid().ToString('N').Substring(0,8))
  $runs_dir = Join-Path $repoResolved 'args\data\runs'
  Ensure-Dir $runs_dir

  $run_dir = Join-Path $runs_dir $run_id
  Ensure-Dir $run_dir

  $evidence_dir = Join-Path $run_dir 'evidence'
  Ensure-Dir $evidence_dir
  Init-StepsLog $evidence_dir

  Trace-Step ("RUN_ID={0}" -f $run_id)
  Trace-Step ("KIT_ID={0}" -f $KitId)

  $requested_product_id = (CoalesceStr $ProductId).Trim()
  $requested_summary    = (CoalesceStr $Summary).Trim()

  # 1) kit_registry_v1 -> job_request.json
  $jr_path        = Join-Path $run_dir 'job_request.json'
  $jr_stdout_path = Join-Path $evidence_dir 'kit_registry_v1.stdout.txt'
  $jr_stderr_path = Join-Path $evidence_dir 'kit_registry_v1.stderr.txt'

  $cmdArgs = @("-3.11","-m","args.foundry.kit_registry_v1","--kit-id",$KitId)
  if ($requested_product_id.Length -gt 0) { $cmdArgs += @("--product-id",$requested_product_id) }
  if ($requested_summary.Length -gt 0)    { $cmdArgs += @("--summary",$requested_summary) }

  $kr = Invoke-PyModule -Args $cmdArgs -StdoutPath $jr_stdout_path -StderrPath $jr_stderr_path -StepName "kit_registry_v1"

  # persist job_request.json as UTF-8
  $jr_stdout = ""
  if (Test-Path -Path $jr_stdout_path) { $jr_stdout = (Get-Content -Raw -Encoding UTF8 -Path $jr_stdout_path) }
  $jr_stdout | Set-Content -Encoding utf8 -Path $jr_path

  if ($kr.rc -ne 0) {
    Emit-AndExit ([ordered]@{
      schema='factory_prompt_v2'; ts_utc=$ts_utc; repo=$repoResolved; step='kit_registry_v1'
      run_id=$run_id; run_dir=$run_dir; kit_id=$KitId
      overrides=[ordered]@{
        requested=[ordered]@{
          product_id=$(if ($requested_product_id) { $requested_product_id } else { "not provided" })
          summary=$(if ($requested_summary) { $requested_summary } else { "not provided" })
        }
      }
      error=("kit_registry_v1 failed (rc={0})" -f $kr.rc)
      stderr_path=$jr_stderr_path; stdout_path=$jr_stdout_path; job_request_path=$jr_path
      evidence=[ordered]@{ evidence_dir=$evidence_dir; prompt_steps_log=$script:StepsLog }
    }) $RC_FAIL
  }

  Trace-Step "BEFORE job_request_parse"
  $jr_obj = Safe-ReadJson $jr_path
  Trace-Step "AFTER job_request_parse"

  $effective_product_id = ""
  $effective_summary    = ""
  try {
    $effective_product_id = [string]$jr_obj.product_id
    if ($null -ne $jr_obj.requirements) { $effective_summary = [string]$jr_obj.requirements.summary }
  } catch { }

  if ([string]::IsNullOrWhiteSpace($effective_product_id)) { $effective_product_id="(missing product_id in job_request)" }
  if ([string]::IsNullOrWhiteSpace($effective_summary))    { $effective_summary="(missing requirements.summary in job_request)" }

  # 2) validate job_request
  $jr_val_path        = Join-Path $run_dir 'job_request_validate.json'
  $jr_val_stdout_path = Join-Path $evidence_dir 'job_request_validate_v1.stdout.txt'
  $jr_val_stderr_path = Join-Path $evidence_dir 'job_request_validate_v1.stderr.txt'

  $valArgs = @("-3.11","-m","args.foundry.job_request_validate_v1","--job-request",$jr_path,"--out",$jr_val_path)
  $vr = Invoke-PyModule -Args $valArgs -StdoutPath $jr_val_stdout_path -StderrPath $jr_val_stderr_path -StepName "job_request_validate_v1"

  if ($vr.rc -ne 0) {
    Emit-AndExit ([ordered]@{
      schema='factory_prompt_v2'; ts_utc=$ts_utc; repo=$repoResolved; step='job_request_validate_v1'
      run_id=$run_id; run_dir=$run_dir; kit_id=$KitId
      overrides=[ordered]@{
        requested=[ordered]@{
          product_id=$(if ($requested_product_id) { $requested_product_id } else { "not provided" })
          summary=$(if ($requested_summary) { $requested_summary } else { "not provided" })
        }
        effective=[ordered]@{ product_id=$effective_product_id; summary=$effective_summary }
      }
      error=("job_request_validate_v1 failed (rc={0})" -f $vr.rc)
      stderr_path=$jr_val_stderr_path; stdout_path=$jr_val_stdout_path
      job_request_path=$jr_path; job_request_validate_path=$jr_val_path
      evidence=[ordered]@{ evidence_dir=$evidence_dir; prompt_steps_log=$script:StepsLog }
    }) $RC_FAIL
  }

  # 3) allowed_paths + template + prompt
  $allowed = @()
  try { foreach ($p in $jr_obj.allowed_paths) { $allowed += [string]$p } } catch { $allowed=@() }

  if ($allowed.Count -le 0) {
    Emit-AndExit ([ordered]@{
      schema='factory_prompt_v2'; ts_utc=$ts_utc; repo=$repoResolved; step='allowed_paths'
      run_id=$run_id; run_dir=$run_dir; kit_id=$KitId
      error="allowed_paths is empty in job_request"
      job_request_path=$jr_path
      evidence=[ordered]@{ evidence_dir=$evidence_dir; prompt_steps_log=$script:StepsLog }
    }) $RC_FAIL
  }

  Trace-Step ("ALLOWED_COUNT={0}" -f $allowed.Count)

  # Global caps to prevent ConvertTo-Json stalls
  $maxFiles = 250
  $maxTotalChars = 2000000  # ~2M chars total content across ops
  $totalChars = 0

  Trace-Step "BEFORE template_prefill_loop"

  $ops = @()
  $i = 0
  foreach ($p in $allowed) {
    $i++
    if ($i -gt $maxFiles) {
      Trace-Step ("CAP_MAX_FILES hit at {0}, skipping rest" -f $maxFiles)
      break
    }

    Trace-Step ("READ_TEMPLATE {0}" -f $p)
    $content = Read-TemplatePackContent -RepoRoot $repoResolved -KitId $KitId -RelPath $p
    if ($null -eq $content) { $content = "" }

    # enforce global size cap
    $cLen = $content.Length
    if (($totalChars + $cLen) -gt $maxTotalChars) {
      Trace-Step ("CAP_TOTAL_CHARS hit, blanking content for {0}" -f $p)
      $content = ""
      $cLen = 0
    }

    $totalChars += $cLen
    Trace-Step ("READ_TEMPLATE_DONE {0} len={1} total={2}" -f $p, $cLen, $totalChars)

    $ops += [ordered]@{ op="write_file"; path=$p; encoding="utf-8"; content=$content }
  }

  Trace-Step "AFTER template_prefill_loop"

  $tmpl_obj  = [ordered]@{ schema="codegen_patch_v0"; ops=$ops }
  $tmpl_path = Join-Path $run_dir 'codegen_output.template.json'

  Trace-Step "BEFORE write_template_json"
  (ConvertTo-Json -InputObject $tmpl_obj -Depth 20) | Set-Content -Encoding utf8 -Path $tmpl_path
  Trace-Step "AFTER write_template_json"

  $jr_raw = Get-Content -Raw -Encoding UTF8 -Path $jr_path

  $requestedPidText = $(if ($requested_product_id) { $requested_product_id } else { "not provided" })
  $requestedSumText = $(if ($requested_summary)    { $requested_summary }    else { "not provided" })

  $prompt = @()
  $prompt += ("OVERRIDES`nrequested ProductId: {0}`neffective  ProductId: {1}`nrequested Summary:   {2}`neffective  Summary:  {3}" -f `
    $requestedPidText, $effective_product_id, $requestedSumText, $effective_summary)
  $prompt += ''
  $prompt += 'ROLE: ChatGPT as codegen tool'
  $prompt += 'OUTPUT MUST BE A SINGLE JSON OBJECT, schema=codegen_patch_v0'
  $prompt += 'ONLY allowed op: write_file'
  $prompt += 'All paths are relative to workspace root. No absolute paths. No .. traversal. No secrets.'
  $prompt += ''
  $prompt += 'JOB_REQUEST_JSON:'
  $prompt += $jr_raw
  $prompt += ''
  $prompt += 'ALLOWED_PATHS (YOU MUST ONLY WRITE THESE PATHS):'
  foreach ($p in $allowed) { $prompt += (" - " + $p) }
  $prompt += ''
  $prompt += 'RESPONSE FORMAT (SINGLE JSON):'
  $prompt += '{"schema":"codegen_patch_v0","ops":[{"op":"write_file","path":"<one_of_allowed_paths>","encoding":"utf-8","content":"..."}]}'

  $prompt_path = Join-Path $run_dir 'prompt_pack.txt'
  $prompt -join "`n" | Set-Content -Encoding utf8 -Path $prompt_path

  Trace-Step "BEFORE emit_ok"

  $out = [ordered]@{
    schema='factory_prompt_v2'
    ts_utc=$ts_utc
    repo=$repoResolved
    run_id=$run_id
    run_dir=$run_dir
    kit_id=$KitId

    overrides=[ordered]@{
      requested=[ordered]@{ product_id=$requestedPidText; summary=$requestedSumText }
      effective=[ordered]@{ product_id=$effective_product_id; summary=$effective_summary }
    }

    requested_product_id=$requestedPidText
    requested_summary=$requestedSumText
    effective_product_id=$effective_product_id
    effective_summary=$effective_summary
    product_id=$effective_product_id

    prompt_path=$prompt_path
    template_path=$tmpl_path
    job_request_path=$jr_path
    job_request_validate_path=$jr_val_path
    allowed_paths_count=$allowed.Count

    caps=[ordered]@{
      max_files=$maxFiles
      max_total_chars=$maxTotalChars
      total_chars=$totalChars
      files_written=$ops.Count
    }

    evidence=[ordered]@{
      evidence_dir=$evidence_dir
      prompt_steps_log=$script:StepsLog
      kit_registry_stdout_path=$jr_stdout_path
      kit_registry_stderr_path=$jr_stderr_path
      job_request_validate_stdout_path=$jr_val_stdout_path
      job_request_validate_stderr_path=$jr_val_stderr_path
    }

    next_step=("Place ChatGPT response JSON into {0}\codegen_output.json, then run: powershell -ExecutionPolicy Bypass -File .\scripts\run_factory_app_build_release_v1.ps1 -RunId {1}" -f $run_dir, $run_id)
  }

  Emit-AndExit $out $RC_OK
}
catch {
  Emit-AndExit ([ordered]@{
    schema='factory_prompt_v2'
    ts_utc=(UtcNowIso)
    repo=$repoResolved
    step='unhandled_exception'
    error=[ordered]@{ kind='exception'; message=$_.Exception.Message }
  }) $RC_INFRA
}
finally {
  try { Pop-Location } catch { }
}
