Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-UtcIso { ([DateTime]::UtcNow.ToString('o')) }

function Tail-Text {
  param([string]$Text, [int]$MaxChars = 2400)
  if (-not $Text) { return $null }
  if ($Text.Length -le $MaxChars) { return $Text }
  return $Text.Substring($Text.Length - $MaxChars)
}

function Resolve-Python {
  $candidates = @(
    @{ exe='py'; args=@('-3.11') },
    @{ exe='py'; args=@() },
    @{ exe='python'; args=@() }
  )
  foreach ($c in $candidates) {
    try {
      $null = Get-Command $c.exe -ErrorAction Stop
      $out = & $c.exe @($c.args + @('-c', 'import sys; print(sys.version)')) 2>$null
      if ($LASTEXITCODE -eq 0 -and $out) { return $c }
    } catch { }
  }
  return $null
}

function Run-PyCapture {
  param(
    [Parameter(Mandatory=$true)][string]$PyExe,
    [Parameter(Mandatory=$true)][string[]]$PyBase,
    [Parameter(Mandatory=$true)][string[]]$Args
  )

  # IMPORTANT (PowerShell 5.1): native stderr is written to error stream.
  # With $ErrorActionPreference='Stop' it becomes a terminating error.
  # We temporarily relax it to capture output and decide by $LASTEXITCODE.
  $old = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    $out = & $PyExe @($PyBase + $Args) 2>&1
    $rc = $LASTEXITCODE
    $txt = ($out | Out-String)
    return [ordered]@{ rc = $rc; out = $txt }
  }
  finally {
    $ErrorActionPreference = $old
  }
}

$ts = Get-UtcIso
$result = [ordered]@{
  schema = 'bootstrap_tools_v1'
  impl = 'bootstrap_tools_ps5_v1_5'
  ts_utc = $ts
  ok = $false
  exit_code = 2
  python = $null
  pip = $null
  tools = [ordered]@{ ruff = $null; pyinstaller = $null }
  actions = @()
  pip_tail = $null
  error = $null
}

try {
  $py = Resolve-Python
  if (-not $py) {
    $result.error = 'Python not found (expected: py or python in PATH)'
    $result | ConvertTo-Json -Depth 12 -Compress
    exit 2
  }

  $pyExe = $py.exe
  $pyBase = [string[]]$py.args

  $pyPath = & $pyExe @($pyBase + @('-c', 'import sys; print(sys.executable)'))
  if ($LASTEXITCODE -ne 0) { throw 'Failed to query python executable' }
  $result.python = ($pyPath | Select-Object -First 1)

  $pipVer = & $pyExe @($pyBase + @('-m','pip','--version'))
  if ($LASTEXITCODE -ne 0) { throw 'pip not available' }
  $result.pip = ($pipVer | Select-Object -First 1)

  # ---- RUFF ----
  $result.actions += 'check: ruff'
  $chkR = Run-PyCapture -PyExe $pyExe -PyBase $pyBase -Args @('-m','ruff','--version')
  if ($chkR.rc -eq 0) {
    $result.tools.ruff = $chkR.out.Trim()
    $result.actions += 'ruff_detected'
  } else {
    $result.actions += 'install: ruff'
    $instR = Run-PyCapture -PyExe $pyExe -PyBase $pyBase -Args @('-m','pip','install','-U','ruff','--disable-pip-version-check','--no-input')
    if ($instR.rc -ne 0) {
      $result.actions += 'install: ruff (--user fallback)'
      $instR2 = Run-PyCapture -PyExe $pyExe -PyBase $pyBase -Args @('-m','pip','install','-U','ruff','--user','--disable-pip-version-check','--no-input')
      $result.pip_tail = Tail-Text -Text ($instR.out + "\n---\n" + $instR2.out)
      if ($instR2.rc -ne 0) { throw 'pip install -U ruff failed' }
    }
    $chkR2 = Run-PyCapture -PyExe $pyExe -PyBase $pyBase -Args @('-m','ruff','--version')
    if ($chkR2.rc -ne 0) {
      $result.pip_tail = Tail-Text -Text (($result.pip_tail + "\n" + $chkR2.out))
      throw 'ruff still not available after install'
    }
    $result.tools.ruff = $chkR2.out.Trim()
  }

  # ---- PYINSTALLER ----
  $result.actions += 'check: pyinstaller'
  $chkP = Run-PyCapture -PyExe $pyExe -PyBase $pyBase -Args @('-m','PyInstaller','--version')
  if ($chkP.rc -eq 0) {
    $result.tools.pyinstaller = $chkP.out.Trim()
    $result.actions += 'pyinstaller_detected'
  } else {
    $result.actions += 'install: pyinstaller'
    $instP = Run-PyCapture -PyExe $pyExe -PyBase $pyBase -Args @('-m','pip','install','-U','pyinstaller','--disable-pip-version-check','--no-input')
    if ($instP.rc -ne 0) {
      $result.actions += 'install: pyinstaller (--user fallback)'
      $instP2 = Run-PyCapture -PyExe $pyExe -PyBase $pyBase -Args @('-m','pip','install','-U','pyinstaller','--user','--disable-pip-version-check','--no-input')
      $result.pip_tail = Tail-Text -Text (($result.pip_tail + "\n" + $instP.out + "\n---\n" + $instP2.out))
      if ($instP2.rc -ne 0) { throw 'pip install -U pyinstaller failed' }
    }
    $chkP2 = Run-PyCapture -PyExe $pyExe -PyBase $pyBase -Args @('-m','PyInstaller','--version')
    if ($chkP2.rc -ne 0) {
      $result.pip_tail = Tail-Text -Text (($result.pip_tail + "\n" + $chkP2.out))
      throw 'PyInstaller still not available after install'
    }
    $result.tools.pyinstaller = $chkP2.out.Trim()
  }

  $result.ok = $true
  $result.exit_code = 0
  $result | ConvertTo-Json -Depth 12 -Compress
  exit 0
}
catch {
  $result.ok = $false
  $result.exit_code = 2
  $result.error = ($_.Exception.Message)
  $result | ConvertTo-Json -Depth 12 -Compress
  exit 2
}
