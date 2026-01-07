$ErrorActionPreference = "Stop"

Set-Location -LiteralPath "C:\Users\mukol\ARGS-Engine-Foundry-v0"

powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\factory_build_window_m8_v1.ps1 `
  -PythonExe "C:\Users\mukol\AppData\Local\Python\bin\python.exe"
