# Product Standard v1 (ARGS-ENGINEER / Software Factory)

## 0) Non-negotiables
- Stdout runners: ровно один JSON (без лишнего текста/traceback).
- Exit codes: 0 OK, 1 FAIL, 2 INFRA.
- Evidence: каждый шаг пишет в args\data\runs\<run_id>\evidence\ + events.jsonl + final_report.json (PASS/FAIL всегда).
- Encoding: допускается BOM; чтение через utf-8-sig или BOM-strip.
- Execution: запуск из корня репо (или абсолютными путями).

## 1) Canonical paths
- Run dir: args\data\runs\<run_id>\
  - events.jsonl
  - final_report.json
  - evidence\<step>.stdout.txt / <step>.stderr.txt
- Dist: dist\<product_id>\ (app.exe + build logs + acceptance artifacts)
- Release: dist\releases\<release_id>.zip (self-describing)

## 2) Self-describing release (minimum)
Release zip содержит acceptance артефакты (если были):
- acceptance_gate.json
- acceptance_gate.stdout.txt
- acceptance_gate.stderr.txt

release_hashes_v0 обязателен:
- included_files[]
- file_sha256{path->sha256}

## 3) Debug by evidence
1) Возьми run_id из JSON stdout
2) args\data\runs\<run_id>\final_report.json
3) args\data\runs\<run_id>\events.jsonl
4) evidence\<step>.stderr.txt для шага с FAIL/INFRA
5) Один минимальный фикс → один прогон

## 4) Golden commands
Prompt:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_factory_app_prompt_v2.ps1 -KitId "kit_cli_tool_v1" -ProductId "demo_cli_tool" -Summary "Demo CLI tool"

BuildRelease (acceptance ON):
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_factory_app_build_release_v1.ps1 -RunId <run_id> -RunAcceptance "YES"

Acceptance on release zip:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_acceptance_gate_v1.ps1 -ReleaseId "<release_id>"
