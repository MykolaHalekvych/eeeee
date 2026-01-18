from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "workspace_gate_v0"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str], cwd: Path | None = None) -> dict[str, Any]:
    p = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False
    )
    return {
        "cmd": cmd,
        "rc": p.returncode,
        "stdout": p.stdout or "",
        "stderr": p.stderr or "",
    }


def py_compile_tree(root: Path) -> dict[str, Any]:
    import py_compile

    py_files = sorted(root.rglob("*.py"))
    errors: list[dict[str, str]] = []
    for f in py_files:
        try:
            py_compile.compile(str(f), doraise=True)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": str(f), "error": repr(e)})
    return {
        "files": [str(p) for p in py_files],
        "errors": errors,
        "ok": len(errors) == 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate a workspace (py_compile + ruff)")
    ap.add_argument("--workspace", required=True)
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    if not ws.exists():
        raise FileNotFoundError(f"workspace not found: {ws}")

    checks: dict[str, Any] = {}
    checks["py_compile"] = py_compile_tree(ws)
    checks["ruff"] = run([sys.executable, "-m", "ruff", "check", str(ws)])

    ok = bool(checks["py_compile"]["ok"]) and checks["ruff"]["rc"] == 0

    out = {
        "schema": SCHEMA,
        "ok": ok,
        "exit_code": 0 if ok else 1,
        "ts_utc": utc_ts(),
        "workspace": str(ws),
        "checks": checks,
    }
    print(json.dumps(out, ensure_ascii=False))
    return out["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
