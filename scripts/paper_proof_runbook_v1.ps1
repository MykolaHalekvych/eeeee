<#
.SYNOPSIS
  Paper Proof Runbook v1 (read-only)

.DESCRIPTION
  Prints current gates (control_plane + stop.flag) and prints canonical commands
  for supervised terminal proofs:
    1) scenario_cancelled_v1
    2) scenario_rejected_v1
    3) scenario_fill_v1 (EXIT-ONLY if position!=0)
    4) scenario_fill_v1 roundtrip (BUY->SELL) if position==0 (requires HALT + confirm-roundtrip)

  Safety:
    - DOES NOT modify any files
    - DOES NOT execute trading commands
#>

[CmdletBinding()]
param(
  [string]$Repo = "C:\Users\mukol\ARGS-Core-v1",
  [string]$ControlPlaneRel = "args\data\control_plane.json",
  [string]$ContractRel = "args\data\ibkr_mhg_contract_v1.json",
  [string]$Symbol = "MHG",
  [int]$Port = 7497,
  [string]$IbHost = "localhost",
  [int]$ClientId = 79
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Read-Json([string]$Path) {
  $raw = Get-Content -Raw -Encoding utf8 $Path
  try { return $raw | ConvertFrom-Json } catch {
    # best effort: utf8-sig
    $raw2 = Get-Content -Raw -Encoding utf8BOM $Path
    return $raw2 | ConvertFrom-Json
  }
}

$repoPath = (Resolve-Path $Repo).Path
$cpPath = Join-Path $repoPath $ControlPlaneRel
$contractPath = Join-Path $repoPath $ContractRel
$stopFlag = Join-Path $repoPath "args\logs\stop.flag"
$posSnap = Join-Path $repoPath "args\data\ibkr_positions_snapshot_v0.json"

Write-Host ""
Write-Host "=== ARGS Paper Proof Runbook v1 (READ-ONLY) ==="
Write-Host "Repo: $repoPath"
Write-Host "ControlPlane: $cpPath"
Write-Host "Contract: $contractPath"
Write-Host "Symbol: $Symbol"
Write-Host "StopFlag: $stopFlag"
Write-Host ""

$cp = Read-Json $cpPath

$executionMode = ($cp.execution_mode | ForEach-Object { "$_" }).ToUpper()
$globalMode = ($cp.global_mode | ForEach-Object { "$_" }).ToUpper()
$enablePaper = [bool]$cp.enable_paper_execution

$stopExists = Test-Path $stopFlag
$contractExists = Test-Path $contractPath
$posExists = Test-Path $posSnap

Write-Host "GATES:"
Write-Host ("  stop.flag exists           : " + $stopExists)
Write-Host ("  execution_mode             : " + $executionMode)
Write-Host ("  enable_paper_execution     : " + $enablePaper)
Write-Host ("  global_mode                : " + $globalMode)
Write-Host ("  contract exists            : " + $contractExists + " (" + $ContractRel + ")")
Write-Host ("  positions snapshot exists  : " + $posExists + " (args\data\ibkr_positions_snapshot_v0.json)")
Write-Host ""

$canExecute = ($stopExists -and $executionMode -eq "PAPER" -and $enablePaper)

Write-Host "EVAL:"
Write-Host ("  can_execute (paper gate)   : " + $canExecute + "  (requires stop.flag + execution_mode=PAPER + enable_paper_execution=true)")
Write-Host ""

Write-Host "CANONICAL COMMANDS (NOT EXECUTED):"
Write-Host ""
Write-Host "1) CANCELLED (requires global_mode=HALT when PAPER enabled)"
Write-Host ("py -3.11 -m args.stage5.terminal_scenarios.terminal_scenarios_v1 " +
            "--repo `"$repoPath`" --scenario scenario_cancelled_v1 --confirm-paper " +
            "--contract-json `"$ContractRel`" --symbol $Symbol --lmt-price 1.0")
Write-Host ""
Write-Host "2) REJECTED (requires global_mode=HALT when PAPER enabled)"
Write-Host ("py -3.11 -m args.stage5.terminal_scenarios.terminal_scenarios_v1 " +
            "--repo `"$repoPath`" --scenario scenario_rejected_v1 --confirm-paper " +
            "--contract-json `"$ContractRel`" --symbol $Symbol")
Write-Host ""
Write-Host "3) FILL EXIT-ONLY (requires position != 0; allows ONLY_EXITS or HALT; requires --confirm-fill YES)"
Write-Host ("py -3.11 -m args.stage5.terminal_scenarios.terminal_scenarios_v1 " +
            "--repo `"$repoPath`" --scenario scenario_fill_v1 --confirm-paper --confirm-fill YES " +
            "--contract-json `"$ContractRel`" --symbol $Symbol")
Write-Host ""
Write-Host "   If positions snapshot missing, generate it (example):"
Write-Host ("py -3.11 -m args.ibkr.ibkr_positions_snapshotter_v0 --host $IbHost --port $Port --client-id $ClientId --timeout-s 25")
Write-Host ""
Write-Host "4) FILL ROUNDTRIP (position==0 only; requires global_mode=HALT + --confirm-roundtrip YES + --confirm-fill YES)"
Write-Host ("py -3.11 -m args.stage5.terminal_scenarios.terminal_scenarios_v1 " +
            "--repo `"$repoPath`" --scenario scenario_fill_v1 --confirm-paper --confirm-fill YES --confirm-roundtrip YES --roundtrip-qty 1 " +
            "--contract-json `"$ContractRel`" --symbol $Symbol")
Write-Host ""

Write-Host "NOTE:"
Write-Host "  This script never enables execution. To enable PAPER, you must explicitly set:"
Write-Host "    control_plane.json: execution_mode='PAPER', enable_paper_execution=true"
Write-Host "  And for CANCELLED/REJECTED/ROUNDTRIP, set global_mode='HALT'."
Write-Host ""
