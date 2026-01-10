param(
  [Parameter(Mandatory=$false)][string]$Repo = (Get-Location).Path,
  [Parameter(Mandatory=$false)][string]$ArtifactRoot = "C:\Users\mukol\ARGS_ARTIFACTS\Engine-Foundry-v0"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RC_OK    = 0
$RC_FAIL  = 1
$RC_INFRA = 2

function Emit-AndExit([hashtable]$Obj, [int]$Code) {
  $Obj.exit_code = $Code
  $Obj.ok = ($Code -eq 0)
  ($Obj | ConvertTo-Json -Depth 12 -Compress)
  exit $Code
}

try {
  $out = [ordered]@{
    schema = "artifact_root_audit_v1"
    ts_utc = ([DateTime]::UtcNow.ToString("o"))
    repo = $Repo
    artifact_root = $ArtifactRoot
    junctions = @()
    missing_pins = @()
  }

  $items = @(
    @{ name="dist";       path=(Join-Path $Repo "dist");                expected=(Join-Path $ArtifactRoot "dist") },
    @{ name="runs";       path=(Join-Path $Repo "args\data\runs");      expected=(Join-Path $ArtifactRoot "args_data\runs") },
    @{ name="workspaces"; path=(Join-Path $Repo "args\data\workspaces");expected=(Join-Path $ArtifactRoot "args_data\workspaces") },
    @{ name="tmp";        path=(Join-Path $Repo "args\data\tmp");       expected=(Join-Path $ArtifactRoot "args_data\tmp") }
  )

  foreach ($it in $items) {
    $gi = Get-Item $it.path -Force -ErrorAction Stop
    $isJ = (($gi.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) -and ($gi.LinkType -eq "Junction")
    $t = @()
    try { $t = @($gi.Target) } catch { $t = @() }
    $ok = $isJ -and ($t -contains $it.expected)

    $out.junctions += @{
      name=$it.name; path=$it.path; is_junction=$isJ; target=$t; expected=$it.expected; ok=$ok
    }

    if (-not $ok) { Emit-AndExit $out $RC_FAIL }
  }

  # Pinned releases (golden proofs)
  $releases = Join-Path (Join-Path $Repo "dist") "releases"
  $pins = @(
    "demo_cli_tool__v0.1.0__e1943ba9a8.zip",
    "demo_cli_tool__v0.1.0__e1943ba9a8.hashes.json",
    "demo_cli_tool__v0.1.0__cust_cust_example_v1__6a6d71b217.zip",
    "demo_cli_tool__v0.1.0__cust_cust_example_v1__6a6d71b217.hashes.json"
  )

  foreach ($p in $pins) {
    $pp = Join-Path $releases $p
    if (-not (Test-Path $pp)) { $out.missing_pins += $pp }
  }

  if ($out.missing_pins.Count -gt 0) { Emit-AndExit $out $RC_FAIL }

  Emit-AndExit $out $RC_OK
}
catch {
  $err = [ordered]@{
    schema="artifact_root_audit_v1"
    ts_utc=([DateTime]::UtcNow.ToString("o"))
    repo=$Repo
    artifact_root=$ArtifactRoot
    ok=$false
    exit_code=$RC_INFRA
    error=@{ kind="exception"; message=$_.Exception.Message }
  }
  ($err | ConvertTo-Json -Depth 6 -Compress)
  exit $RC_INFRA
}