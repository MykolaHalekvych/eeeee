@echo off
setlocal
cd /d %~dp0\..
py -3.11 -m args.foundry.build_exe_v0 --product-id windows_cli_tool_v0
exit /b %errorlevel%
