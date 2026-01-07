from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List

from src.canonical_json_v1 import dumps_canonical
from src.service_core import deterministic_tick, service_status

APP_VERSION = "0.1.0"


def cmd_ping(_args: argparse.Namespace) -> Dict[str, Any]:
    return {"ok": True, "cmd": "ping"}


def cmd_version(_args: argparse.Namespace) -> Dict[str, Any]:
    return {"ok": True, "cmd": "version", "version": APP_VERSION}


def cmd_selftest(_args: argparse.Namespace) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    checks.append({"name": "ping", "ok": cmd_ping(argparse.Namespace()).get("ok") is True})
    checks.append({"name": "version", "ok": isinstance(cmd_version(argparse.Namespace()).get("version"), str)})

    st = service_status()
    checks.append({"name": "service_status", "ok": bool(st.get("ok")) and st.get("state") == "READY"})

    tk = deterministic_tick()
    checks.append({"name": "deterministic_tick", "ok": bool(tk.get("ok")) and tk.get("tick") == 1})

    ok = all(bool(c["ok"]) for c in checks)
    return {"ok": ok, "cmd": "selftest", "checks": checks}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app", add_help=True)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ping")
    sub.add_parser("version")
    sub.add_parser("selftest")

    return p


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)

    try:
        if ns.command == "ping":
            out = cmd_ping(ns)
        elif ns.command == "version":
            out = cmd_version(ns)
        elif ns.command == "selftest":
            out = cmd_selftest(ns)
        else:
            out = {"ok": False, "error": f"unknown command: {ns.command}"}

        sys.stdout.write(dumps_canonical(out) + os.linesep)
        return 0 if out.get("ok") else 1
    except Exception as ex:
        sys.stderr.write(f"ERROR: {ex}{os.linesep}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
