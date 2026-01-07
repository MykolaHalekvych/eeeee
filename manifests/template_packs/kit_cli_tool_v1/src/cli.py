from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from src.canonical_json_v1 import dumps_canonical
from src.product_manifest_v1 import validate_manifest

APP_VERSION = "0.1.0"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def cmd_ping(_args: argparse.Namespace) -> Dict[str, Any]:
    return {"ok": True, "cmd": "ping"}


def cmd_version(_args: argparse.Namespace) -> Dict[str, Any]:
    return {"ok": True, "cmd": "version", "version": APP_VERSION}


def cmd_hash(_args: argparse.Namespace) -> Dict[str, Any]:
    # Deterministic: hash of version + python major/minor (stable across runs)
    payload = f"{APP_VERSION}|py{sys.version_info.major}.{sys.version_info.minor}".encode("utf-8")
    h = hashlib.sha256(payload).hexdigest()
    return {"ok": True, "cmd": "hash", "sha256": h}


def cmd_manifest_validate(args: argparse.Namespace) -> Dict[str, Any]:
    path = Path(args.path).resolve()
    raw = _read_text(path)
    obj = json.loads(raw)
    ok, errs = validate_manifest(obj)
    return {"ok": ok, "cmd": "manifest-validate", "path": str(path), "errors": errs}


def cmd_selftest(_args: argparse.Namespace) -> Dict[str, Any]:
    # Deterministic internal checks; no network, no time.
    checks: List[Dict[str, Any]] = []
    checks.append({"name": "ping", "ok": cmd_ping(argparse.Namespace()).get("ok") is True})
    checks.append({"name": "version", "ok": isinstance(cmd_version(argparse.Namespace()).get("version"), str)})
    checks.append({"name": "hash", "ok": len(cmd_hash(argparse.Namespace()).get("sha256", "")) == 64})
    ok = all(bool(c["ok"]) for c in checks)
    return {"ok": ok, "cmd": "selftest", "checks": checks}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app", add_help=True)
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("ping")
    sub.add_parser("version")
    sub.add_parser("hash")
    sub.add_parser("selftest")

    mv = sub.add_parser("manifest-validate")
    mv.add_argument("--path", required=True, help="Path to product_manifest_v1 JSON")

    return p


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)

    try:
        if ns.command == "ping":
            out = cmd_ping(ns)
        elif ns.command == "version":
            out = cmd_version(ns)
        elif ns.command == "hash":
            out = cmd_hash(ns)
        elif ns.command == "selftest":
            out = cmd_selftest(ns)
        elif ns.command == "manifest-validate":
            out = cmd_manifest_validate(ns)
        else:
            out = {"ok": False, "error": f"unknown command: {ns.command}"}

        sys.stdout.write(dumps_canonical(out) + os.linesep)
        return 0 if out.get("ok") else 1
    except Exception as ex:
        sys.stderr.write(f"ERROR: {ex}{os.linesep}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
