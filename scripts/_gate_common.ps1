Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Ensure-Dir([string]$p) {
  if (!(Test-Path $p)) { New-Item -ItemType Directory -Force -Path $p | Out-Null }
}

function Write-Json([object]$obj) {
  $obj | ConvertTo-Json -Depth 30
}

function Write-JsonFile([string]$path, [object]$obj) {
  Ensure-Dir (Split-Path -Parent $path)
  (Write-Json $obj) | Set-Content -Encoding UTF8 $path
}
