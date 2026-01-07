param(
  [Parameter(Mandatory=$true)][string]$JobRequest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function UtcNowId {
  return ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))
}

$repo = (Get-Location).Path

# 0) Validate job_request BEFORE creating run dir
$val = & py -3.11 -m args.foundry.job_request_validate_v1 --job-request $JobRequest
$val_rc = $LASTEXITCODE
if ($val_rc -ne 0) {
  $val
  exit $val_rc
}

$run_id = (UtcNowId) + '_' + ([guid]::NewGuid().ToString('N').Substring(0,8))

$runs_dir = Join-Path $repo 'args\data\runs'
$run_dir = Join-Path $runs_dir $run_id
New-Item -ItemType Directory -Force $run_dir | Out-Null

Copy-Item -Force $JobRequest (Join-Path $run_dir 'job_request.json')

# 1) Re-validate copied file (guards operator mistakes) + save report
$jr_json_path = Join-Path $run_dir 'job_request.json'
$jr_val_path  = Join-Path $run_dir 'job_request_validate.json'
$val2 = & py -3.11 -m args.foundry.job_request_validate_v1 --job-request $jr_json_path --out $jr_val_path
if ($LASTEXITCODE -ne 0) {
  $val2
  exit $LASTEXITCODE
}

# 2) Load job_request object + allowed_paths
$jr_obj = Get-Content $jr_json_path -Raw | ConvertFrom-Json
$allowed = @()
foreach ($p in $jr_obj.allowed_paths) { $allowed += [string]$p }

if ($allowed.Count -lt 1) {
  throw "job_request.allowed_paths is empty (must include at least one path)"
}

# 3) Create template for operator convenience (based on allowed_paths)
$ops = @()
foreach ($p in $allowed) {
  $ops += [ordered]@{
    op = "write_file"
    path = $p
    encoding = "utf-8"
    content = ""
  }
}

$tmpl_obj = [ordered]@{
  schema = "codegen_patch_v0"
  ops = $ops
}

$tmpl_path = Join-Path $run_dir 'codegen_output.template.json'
($tmpl_obj | ConvertTo-Json -Depth 12) | Set-Content -Encoding utf8 $tmpl_path

# 4) Build prompt pack (must align with allowlist)
$jr_raw = Get-Content $jr_json_path -Raw

$prompt = @()
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
$prompt += 'IMPORTANT: Any write outside ALLOWED_PATHS MUST NOT appear in your patch.'
$prompt += ''
$prompt += 'RESPONSE FORMAT (SINGLE JSON):'
$prompt += '{"schema":"codegen_patch_v0","ops":[{"op":"write_file","path":"<one_of_allowed_paths>","encoding":"utf-8","content":"..."}]}'

$prompt_path = Join-Path $run_dir 'prompt_pack.txt'
$prompt -join "`n" | Set-Content -Encoding utf8 $prompt_path

# 5) Output summary for operator
$out = [ordered]@{
  schema = 'factory_prompt_v1'
  ok = $true
  exit_code = 0
  run_id = $run_id
  run_dir = $run_dir
  prompt_path = $prompt_path
  template_path = $tmpl_path
  job_request_path = $jr_json_path
  job_request_validate_path = $jr_val_path
  allowed_paths_count = $allowed.Count
  next_step = ("Place ChatGPT response JSON into {0}\codegen_output.json, then run: powershell -ExecutionPolicy Bypass -File .\scripts\run_factory_app_build_release_v1.ps1 -RunId {1}" -f $run_dir, $run_id)
}
$out | ConvertTo-Json -Depth 8 -Compress
exit 0
