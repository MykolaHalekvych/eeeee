@echo off
setlocal
cd /d C:\Users\mukol\ARGS-Core-v1

py -3.11 -m args.ops.soak_report_v0 --window-hours 168 --expected-interval-s 900 --archive --out args\data\soak_report_7d.json --bootstrap-low-sample-warn > args\logs\scheduler_soakreport_7d.log 2>&1
exit /b %ERRORLEVEL%
