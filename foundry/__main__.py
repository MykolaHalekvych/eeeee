from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import subprocess

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INFRA = 2

def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8-sig"))

def stop_or_halt(cp: dict) -> tuple[bool,str]:
    stop_path = cp.get("global", {}).get("stop_flag_path", "stop.flag")
    if Path(stop_path).exists():
        return True, "STOP_FLAG"
    mode = cp.get("engineering", {}).get("mode", "DRYRUN")
    if mode == "HALT":
        return True, "HALT_MODE"
    return False, ""

def plan(args) -> int:
    cp = load_json(Path(args.control_plane))
    blocked, reason = stop_or_halt(cp)
    out = {"ok": False, "cmd": "plan", "blocked": blocked, "reason": reason, "factories": args.factories}
    if blocked:
        print(json.dumps(out, ensure_ascii=False))
        return EXIT_INFRA
    # v0: просто печатаем план по factory_id (позже читаем manifests/factories.json)
    out["ok"] = True
    out["plan"] = [{"factory_id": f, "action": "BUILD_BUNDLE", "mode_required": "ALLOW_BUILD"} for f in args.factories]
    print(json.dumps(out, ensure_ascii=False))
    return EXIT_PASS

def gate(args) -> int:
    cp = load_json(Path(args.control_plane))
    blocked, reason = stop_or_halt(cp)
    mode = cp.get("engineering", {}).get("mode", "DRYRUN")
    out = {"ok": False, "cmd": "gate", "blocked": blocked, "reason": reason, "mode": mode, "factories": args.factories}
    if blocked:
        print(json.dumps(out, ensure_ascii=False))
        return EXIT_INFRA

    # v0 gate: py_compile (compileall) + unit tests (unittest) + lockfile check
    rc = 0
    checks = []

    # compileall
    c = subprocess.run([sys.executable, "-m", "compileall", "foundry"], capture_output=True, text=True)
    checks.append({"name": "py_compile", "ok": c.returncode == 0})
    if c.returncode != 0:
        rc = EXIT_FAIL

    # unit tests (only for packs/observability-pack-v0/tests in v0)
    tpath = Path("packs/observability-pack-v0/tests")
    if tpath.exists():
        t = subprocess.run([sys.executable, "-m", "unittest", "discover", str(tpath)], capture_output=True, text=True)
        checks.append({"name": "unit_tests", "ok": t.returncode == 0})
        if t.returncode != 0:
            rc = EXIT_FAIL
    else:
        checks.append({"name": "unit_tests", "ok": False, "reason": "MISSING_TESTS_PATH"})
        rc = EXIT_FAIL

    # dependency pin/lock: минимально — requirements.txt должен быть
    req = Path("requirements.txt")
    checks.append({"name": "dependency_pin", "ok": req.exists()})
    if not req.exists():
        rc = EXIT_FAIL

    out["checks"] = checks
    out["ok"] = (rc == 0)
    print(json.dumps(out, ensure_ascii=False))
    return rc

def main() -> int:
    ap = argparse.ArgumentParser(prog="foundry")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p1 = sp.add_parser("plan")
    p1.add_argument("--factories", nargs="+", required=True)
    p1.add_argument("--control-plane", required=True)
    p1.set_defaults(fn=plan)

    p2 = sp.add_parser("gate")
    p2.add_argument("--factories", nargs="+", required=True)
    p2.add_argument("--control-plane", required=True)
    p2.set_defaults(fn=gate)

    args = ap.parse_args()
    return args.fn(args)

if __name__ == "__main__":
    raise SystemExit(main())
