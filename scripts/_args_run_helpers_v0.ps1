function Get-ArgsRepoRoot {
    param(
        [Parameter(Mandatory=$true)][string]$StartDir
    )

    $dir = (Resolve-Path $StartDir).Path
    while ($true) {
        $guard = Join-Path $dir ".args_engine_repo"
        if (Test-Path $guard) { return $dir }

        $parent = Split-Path -Parent $dir
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $dir) {
            throw "Repo root not found (guard .args_engine_repo). StartDir=$StartDir"
        }
        $dir = $parent
    }
}

function New-ArgsRunId {
    param(
        [Parameter(Mandatory=$true)][string]$RepoRoot
    )

    $runId = & py -3.11 -m args.foundry.runs_v0 new-run-id --repo-root $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw "Failed to generate run_id (runs_v0 new-run-id)." }
    return ($runId | Out-String).Trim()
}

function Initialize-ArgsRun {
    param(
        [Parameter(Mandatory=$true)][string]$RunDir,
        [Parameter(Mandatory=$true)][string]$RunId,
        [Parameter(Mandatory=$true)][string]$RepoRoot,
        [Parameter(Mandatory=$true)][string]$Product,
        [Parameter(Mandatory=$true)][string]$Factory,
        [Parameter(Mandatory=$true)][string]$ControlPlanePath
    )

    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

    & py -3.11 -m args.foundry.runs_v0 init `
        --run-dir $RunDir `
        --run-id $RunId `
        --repo-root $RepoRoot `
        --product $Product `
        --factory $Factory `
        --control-plane $ControlPlanePath | Out-Null

    if ($LASTEXITCODE -ne 0) { throw "Failed to init run (runs_v0 init)." }
}

function Get-ArgsStatusFromExitCode {
    param(
        [Parameter(Mandatory=$true)][int]$ExitCode
    )

    switch ($ExitCode) {
        0 { "PASS" }
        1 { "FAIL" }
        2 { "ERROR" }
        default { "ERROR" }
    }
}

function Add-ArgsRunEvent {
    param(
        [Parameter(Mandatory=$true)][string]$RunDir,
        [Parameter(Mandatory=$true)][string]$Event,
        [Parameter(Mandatory=$true)][string]$Status,
        [Parameter(Mandatory=$true)][int]$ExitCode,
        [Parameter(Mandatory=$false)]$Data
    )

    $args = @(
        "-m","args.foundry.runs_v0","event",
        "--run-dir",$RunDir,
        "--event",$Event,
        "--status",$Status,
        "--exit-code",$ExitCode
    )

    if ($null -ne $Data) {
        $dataJson = $Data | ConvertTo-Json -Depth 20 -Compress
        $args += @("--data-json",$dataJson)
    }

    & py -3.11 @args | Out-Null
    return $LASTEXITCODE
}

function Finalize-ArgsRun {
    param(
        [Parameter(Mandatory=$true)][string]$RunDir,
        [Parameter(Mandatory=$true)][string]$OverallStatus,
        [Parameter(Mandatory=$true)][int]$OverallExitCode,
        [Parameter(Mandatory=$false)]$Seed
    )

    $seedPath = Join-Path $RunDir "seed.json"
    if ($null -ne $Seed) {
        ($Seed | ConvertTo-Json -Depth 30) | Set-Content -Path $seedPath -Encoding UTF8
    }

    $args = @(
        "-m","args.foundry.runs_v0","finalize",
        "--run-dir",$RunDir,
        "--overall-status",$OverallStatus,
        "--overall-exit-code",$OverallExitCode
    )

    if (Test-Path $seedPath) {
        $args += @("--seed-file",$seedPath)
    }

    & py -3.11 @args | Out-Null

    return [pscustomobject]@{
        runs_cli_exit = $LASTEXITCODE
        seed_path = $seedPath
        events_path = (Join-Path $RunDir "events.jsonl")
        final_report_path = (Join-Path $RunDir "final_report.json")
    }
}
