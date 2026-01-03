[CmdletBinding()]
param(
  [string]$Repo = "",
  [int]$WindowHours = 24
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function _utc() { [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ") }
function _parse_ts([string]$s) { if (-not $s) { return $null }; [DateTime]::Parse($s).ToUniversalTime() }

try {
  if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path } else { $Repo = (Resolve-Path -LiteralPath $Repo).Path }
  $cutoff = ([DateTime]::UtcNow).AddHours(-$WindowHours)

  $gateLog = Join-Path $Repo "args\logs\stage7_soak_gate_stdout.log"
  $tapHealth = Join-Path $Repo "args\data\ibkr_event_tap_health_v1.json"
  $evidenceBase = Join-Path $Repo "args\stage5_evidence"
  $proofBase = Join-Path $Repo "args\stage5_terminal_proofs"

  $outBase = Join-Path $Repo "args\ops_evidence\soak_reports\24h"
  New-Item -ItemType Directory -Force -Path $outBase | Out-Null
  $stamp = ([DateTime]::UtcNow).ToString("yyyyMMddTHHmmssZ")
  $outDir = Join-Path $outBase $stamp
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null

  $gate = @()
  if (Test-Path -LiteralPath $gateLog) {
    foreach ($line in Get-Content -LiteralPath $gateLog) {
      $t = $line.Trim()
      if (-not $t.StartsWith("{")) { continue }
      try {
        $o = $t | ConvertFrom-Json
        if ($o.schema -ne "ops_soak_gate_v1") { continue }
        $ts = _parse_ts ([string]$o.ts_utc)
        if ($ts -and $ts -ge $cutoff) {
          $gate += [ordered]@{ ts_utc=[string]$o.ts_utc; status=[string]$o.status; exit_code=[int]$o.exit_code; issues=@($o.issues); warns=@($o.warns) }
        }
      } catch {}
    }
  }

  $counts = [ordered]@{ OK=0; WARN=0; FAIL=0 }
  foreach ($g in $gate) { if ($counts.Contains($g.status)) { $counts[$g.status] += 1 } }

  $worst="OK"; $exit=0
  if ($counts.FAIL -gt 0) { $worst="FAIL"; $exit=2 } elseif ($counts.WARN -gt 0) { $worst="WARN"; $exit=1 }

  $maxGapSec = 0
  $gateSorted = $gate | Sort-Object { _parse_ts $_.ts_utc }
  for ($i=1; $i -lt $gateSorted.Count; $i++) {
    $a=_parse_ts $gateSorted[$i-1].ts_utc; $b=_parse_ts $gateSorted[$i].ts_utc
    if ($a -and $b) { $gap=($b-$a).TotalSeconds; if ($gap -gt $maxGapSec) { $maxGapSec=[int][Math]::Round($gap,0) } }
  }
  $lastGate = $null; if ($gateSorted.Count -gt 0) { $lastGate = $gateSorted[-1] }

  $evidenceDirs=@()
  if (Test-Path -LiteralPath $evidenceBase) {
    foreach ($d in Get-ChildItem -LiteralPath $evidenceBase -Directory) {
      try { $dt=[DateTime]::ParseExact($d.Name,"yyyyMMddTHHmmssZ",$null).ToUniversalTime(); if ($dt -ge $cutoff) { $evidenceDirs += $d.Name } } catch {}
    }
  }
  $evidenceDirs = $evidenceDirs | Sort-Object -Descending

  $proofDirs=@(); $reject=0; $fill=0; $cancel=0
  if (Test-Path -LiteralPath $proofBase) {
    foreach ($d in Get-ChildItem -LiteralPath $proofBase -Directory) {
      if ($d.Name -notmatch '^(\d{8}T\d{6}Z)_terminal$') { continue }
      $dt=[DateTime]::ParseExact($Matches[1],"yyyyMMddTHHmmssZ",$null).ToUniversalTime()
      if ($dt -lt $cutoff) { continue }
      $proofDirs += $d.Name
      $sumPath = Join-Path $d.FullName "proof_summary.json"
      if (Test-Path -LiteralPath $sumPath) {
        try {
          $sum = Get-Content -LiteralPath $sumPath -Raw | ConvertFrom-Json
          foreach ($p in @($sum.proofs)) {
            switch ([string]$p.kind) { "REJECTED"{$reject++} "FILLED"{$fill++} "CANCELLED"{$cancel++} }
          }
        } catch {}
      }
    }
  }
  $proofDirs = $proofDirs | Sort-Object -Descending

  $tap=$null; if (Test-Path -LiteralPath $tapHealth) { try { $tap=Get-Content $tapHealth -Raw | ConvertFrom-Json } catch {} }

  $report=[ordered]@{
    schema="soak_report_24h_v1"; ts_utc=_utc; window_hours=$WindowHours; repo=$Repo; status=$worst; exit_code=$exit;
    gate_samples_total=$gate.Count; gate_counts=$counts; gate_max_gap_sec=$maxGapSec; gate_last=$lastGate;
    tap_health_current=$tap;
    evidence_dirs_count=$evidenceDirs.Count; evidence_dirs_latest=@($evidenceDirs | Select -First 5);
    terminal_proof_dirs_count=$proofDirs.Count; terminal_proof_dirs_latest=@($proofDirs | Select -First 5);
    terminal_proof_counts=[ordered]@{rejected=$reject; filled=$fill; cancelled=$cancel}
  }

  ($report | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath (Join-Path $outDir "soak_report_24h.json") -Encoding UTF8
  ($report | ConvertTo-Json -Depth 12) | Set-Content -LiteralPath (Join-Path $Repo "args\data\soak_report_24h_latest.json") -Encoding UTF8

  ($report | ConvertTo-Json -Compress -Depth 12) | Write-Output
  exit $exit
}
catch {
  $out=[ordered]@{ schema="soak_report_24h_v1"; ts_utc=_utc; status="FAIL"; exit_code=2; error=$_.Exception.Message }
  ($out | ConvertTo-Json -Compress -Depth 6) | Write-Output
  exit 2
}
