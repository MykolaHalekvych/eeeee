param(
  [Parameter(Mandatory=$false)][string]$Zip = "",
  [Parameter(Mandatory=$false)][string]$ReleaseId = "",
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Exit codes (project standard)
$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function UtcNowId  { return ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")) }
function UtcNowIso { return ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")) }

function Ensure-Dir([string]$Path) {
  if (-not (Test-Path -Path $Path -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
  }
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
  $enc = New-Object System.Text.UTF8Encoding($false)
  $dir = Split-Path -Parent $Path
  if ($dir -and -not (Test-Path -Path $dir -PathType Container)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
  }
  [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function Safe-ReadJsonText([string]$Path) {
  $raw = Get-Content -Raw -Encoding UTF8 -Path $Path -ErrorAction Stop
  if ($null -eq $raw) { throw "Empty file: $Path" }
  # BOM-safe
  return ($raw -replace "^\uFEFF","")
}

# ONE canonical emitter (stdout must be exactly one JSON)
function Emit-AndExit {
  param(
    [Parameter(Mandatory=$true)][AllowNull()][object]$Payload,
    [Parameter(Mandatory=$true)][int]$Code
  )

  if ($null -eq $Payload) {
    $Payload = [ordered]@{
      schema = "run_acceptance_gate_v1"
      error  = [ordered]@{ kind="null_payload"; message="Emit-AndExit called with null payload." }
    }
    $Code = $RC_INFRA
  }

  $obj = $null
  if ($Payload -is [System.Collections.IDictionary]) {
    $obj = $Payload
  } else {
    $obj = [ordered]@{ payload = [string]$Payload }
  }

  if (-not $obj.Contains("schema")) { $obj["schema"] = "run_acceptance_gate_v1" }
  if (-not $obj.Contains("ts_utc")) { $obj["ts_utc"] = (UtcNowIso) }

  $obj["exit_code"] = [int]$Code
  $obj["ok"]        = ([int]$Code -eq $RC_OK)

  $json = $null
  try {
    $json = ConvertTo-Json -InputObject $obj -Depth 50 -Compress
  } catch {
    $fallback = [ordered]@{
      schema    = "run_acceptance_gate_v1"
      ts_utc    = (UtcNowIso)
      ok        = $false
      exit_code = $RC_INFRA
      error     = [ordered]@{
        kind="json_serialize_failed"
        message="ConvertTo-Json failed in Emit-AndExit"
        exception=$_.Exception.Message
      }
    }
    $json = ConvertTo-Json -InputObject $fallback -Depth 20 -Compress
    $Code = $RC_INFRA
  }

  if ([string]::IsNullOrWhiteSpace($json)) {
    $json = '{"schema":"run_acceptance_gate_v1","ok":false,"exit_code":2,"error":{"kind":"empty_stdout","message":"Emit-AndExit produced empty JSON"}}'
    $Code = $RC_INFRA
  }

  Write-Output $json
  [Console]::Out.Flush()
  exit $Code
}

function Invoke-PyModule {
  param(
    [Parameter(Mandatory=$true)][string[]]$Args,
    [Parameter(Mandatory=$true)][string]$StdoutPath,
    [Parameter(Mandatory=$true)][string]$StderrPath
  )

  Remove-Item -Force -ErrorAction SilentlyContinue -Path $StdoutPath,$StderrPath

  $py = $null
  try { $py = (Get-Command py -ErrorAction Stop).Source } catch {
    Write-Utf8NoBom $StderrPath "py launcher not found."
    Write-Utf8NoBom $StdoutPath ""
    return [ordered]@{ rc = $RC_INFRA }
  }

  $safeArgs = @()
  foreach ($a in $Args) { if ($null -ne $a) { $safeArgs += ([string]$a) } }

  $rc = $RC_INFRA
  $stdoutText = ""
  try {
    $lines = & $py @safeArgs 2> $StderrPath
    $rc = [int]$LASTEXITCODE
    if ($null -ne $lines) { $stdoutText = ($lines -join "`n") }
  } catch {
    $rc = $RC_INFRA
    $stdoutText = ""
    try { Write-Utf8NoBom $StderrPath $_.Exception.Message } catch { }
  }

  Write-Utf8NoBom $StdoutPath $stdoutText
  return [ordered]@{ rc = [int]$rc }
}

# =========================
# MAIN
# =========================
try {
  $repoResolved = $Repo
  try { $repoResolved = (Resolve-Path -Path $Repo -ErrorAction Stop).Path } catch { }

  if (-not (Test-Path -Path $repoResolved -PathType Container)) {
    Emit-AndExit ([ordered]@{
      schema="run_acceptance_gate_v1"
      step="preflight"
      repo=$repoResolved
      error=[ordered]@{ kind="repo_not_found"; message=("Repo path not found: {0}" -f $repoResolved) }
    }) $RC_INFRA
  }

  Push-Location -Path $repoResolved

  # Resolve zip input
  $zipPath = $Zip.Trim()
  $rid = $ReleaseId.Trim()

  if ([string]::IsNullOrWhiteSpace($zipPath)) {
    if (-not [string]::IsNullOrWhiteSpace($rid)) {
      $zipPath = Join-Path $repoResolved ("dist\releases\" + $rid + ".zip")
    } else {
      Emit-AndExit ([ordered]@{
        schema="run_acceptance_gate_v1"
        step="preflight"
        repo=$repoResolved
        error=[ordered]@{ kind="missing_input"; message="Provide -Zip <path> or -ReleaseId <id>." }
      }) $RC_FAIL
    }
  }

  if (-not (Test-Path -Path $zipPath -PathType Leaf)) {
    Emit-AndExit ([ordered]@{
      schema="run_acceptance_gate_v1"
      step="preflight"
      repo=$repoResolved
      zip=$zipPath
      error=[ordered]@{ kind="zip_not_found"; message=("Zip not found: {0}" -f $zipPath) }
    }) $RC_FAIL
  }

  # Run dirs
  $run_id   = (UtcNowId) + "_" + ([guid]::NewGuid().ToString("N").Substring(0,8))
  $runs_dir = Join-Path $repoResolved "args\data\runs"
  Ensure-Dir $runs_dir

  $run_dir = Join-Path $runs_dir $run_id
  Ensure-Dir $run_dir

  $evidence_dir = Join-Path $run_dir "evidence"
  Ensure-Dir $evidence_dir

  $gate_out_json = Join-Path $run_dir "acceptance_gate_out.json"
  $gate_stdout   = Join-Path $evidence_dir "acceptance_gate_v1.stdout.txt"
  $gate_stderr   = Join-Path $evidence_dir "acceptance_gate_v1.stderr.txt"

  # Call acceptance gate module
  $args = @(
    "-3.11","-m","args.foundry.acceptance_gate_v1",
    "--zip",$zipPath,
    "--out",$gate_out_json
  )

  $res = Invoke-PyModule -Args $args -StdoutPath $gate_stdout -StderrPath $gate_stderr
  $rc = [int]$res.rc

  # Try to parse gate JSON (prefer stdout; fallback to --out)
  $gate_json_text = ""
  $gate_obj = $null
  $parse_ok = $false

  try {
    if (Test-Path -Path $gate_stdout -PathType Leaf) {
      $gate_json_text = Safe-ReadJsonText $gate_stdout
      if (-not [string]::IsNullOrWhiteSpace($gate_json_text)) {
        $gate_obj = ($gate_json_text | ConvertFrom-Json)
        $parse_ok = $true
      }
    }
  } catch { $parse_ok = $false }

  if (-not $parse_ok) {
    try {
      if (Test-Path -Path $gate_out_json -PathType Leaf) {
        $gate_json_text = Safe-ReadJsonText $gate_out_json
        if (-not [string]::IsNullOrWhiteSpace($gate_json_text)) {
          $gate_obj = ($gate_json_text | ConvertFrom-Json)
          $parse_ok = $true
        }
      }
    } catch { $parse_ok = $false }
  }

  # Map rc to project codes (0/1/2). If gate returns other, treat as INFRA.
  $exit_code = $RC_INFRA
  if ($rc -eq 0) { $exit_code = $RC_OK }
  elseif ($rc -eq 1) { $exit_code = $RC_FAIL }
  elseif ($rc -eq 2) { $exit_code = $RC_INFRA }
  else { $exit_code = $RC_INFRA }

  $out = [ordered]@{
    schema="run_acceptance_gate_v1"
    repo=$repoResolved
    run_id=$run_id
    run_dir=$run_dir
    evidence_dir=$evidence_dir
    zip=$zipPath
    gate_rc=$rc
    gate_out_json=$gate_out_json
    gate_stdout_path=$gate_stdout
    gate_stderr_path=$gate_stderr
    gate_parse_ok=$parse_ok
  }

  if ($parse_ok -and $null -ne $gate_obj) {
    $out["gate"] = $gate_obj
    # mirror gate ok/exit_code if present
    try { $out["gate_ok"] = [bool]$gate_obj.ok } catch { }
    try { $out["gate_exit_code"] = [int]$gate_obj.exit_code } catch { }
  } else {
    $out["error"] = [ordered]@{
      kind="gate_json_parse_failed"
      message="Could not parse acceptance_gate_v1 JSON from stdout or --out file."
    }
  }

  Emit-AndExit $out $exit_code
}
catch {
  Emit-AndExit ([ordered]@{
    schema="run_acceptance_gate_v1"
    step="unhandled_exception"
    error=[ordered]@{ kind="exception"; message=$_.Exception.Message }
  }) $RC_INFRA
}
finally {
  try { Pop-Location } catch { }
}
