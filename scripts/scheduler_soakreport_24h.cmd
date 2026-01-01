@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1

py -3.11 -m args.ops.soak_report_v0 --window-hours 24 --expected-interval-s 900 --archive --out args\data\soak_report_24h.json --bootstrap-low-sample-warn > args\logs\scheduler_soakreport_24h.log 2>&1
exit /b %ERRORLEVEL%
