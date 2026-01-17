# templates/cicd_release_pack_v0/src/main.py
# Contract:
# - Emit exactly one JSON line to stdout per invocation (best-effort).
# - Exit codes: 0 OK, 1 FAIL (user error), 2 INFRA (unexpected exception).

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

PRODUCT_ID = "cicd_release_pack_v0"
SCHEMA = "cicd_release_pack_v0_cli_v1"
VERSION = "0.1.0"


def _ts_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_line(payload: Dict[str, Any]) -> str:
    # Single-line JSON for logs
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False) + "\n"


def _os_write_all(fd: int, data: bytes) -> bool:
    try:
        off = 0
        while off < len(data):
            n = os.write(fd, data[off:])
            if n is None or n <= 0:
                return False
            off += n
        return True
    except Exception:
        return False


def _write_stdout(line: str) -> None:
    """
    Critical: try OS-level fd=1 FIRST.
    In PyInstaller windowed/noconsole builds sys.stdout can be a "null sink" that doesn't error.
    If we write to sys.stdout first, we may lose output and never reach fd=1.
    """
    data = line.encode("utf-8", errors="replace")

    # 1) Best: real stdout handle (works with redirection: app.exe --help > file)
    if _os_write_all(1, data):
        return

    # 2) Fallback: sys.stdout
    try:
        out = getattr(sys, "stdout", None)
        if out is not None:
            out.write(line)
            out.flush()
            return
    except Exception:
        pass

    # 3) Last resort: stderr (won't satisfy stdout gate, but better than silence)
    try:
        err = getattr(sys, "stderr", None)
        if err is not None:
            err.write(line)
            err.flush()
            return
    except Exception:
        pass

    _os_write_all(2, data)


def _emit(cmd: str, ok: bool, exit_code: int, reason_code: str, **extra: Any) -> int:
    payload: Dict[str, Any] = {
        "schema": SCHEMA,
        "product_id": PRODUCT_ID,
        "ts_utc": _ts_utc(),
        "cmd": cmd,
        "ok": bool(ok),
        "exit_code": int(exit_code),
        "reason_code": reason_code,
    }
    if extra:
        payload.update(extra)
    _write_stdout(_json_line(payload))
    return int(exit_code)


def _help() -> int:
    return _emit(
        cmd="help",
        ok=True,
        exit_code=0,
        reason_code="CICD_CLI.OK.HELP",
        version=VERSION,
        usage=[
            "app.exe --help",
            "app.exe version",
            "app.exe selftest",
            "app.exe ping",
            "app.exe <unknown>  (rc=1 + JSON)",
        ],
        commands=["--help", "-h", "help", "version", "selftest", "ping"],
    )


def _version() -> int:
    return _emit(
        cmd="version",
        ok=True,
        exit_code=0,
        reason_code="CICD_CLI.OK.VERSION",
        version=VERSION,
    )


def _selftest() -> int:
    checks = [
        {"name": "cli_json_stdout", "ok": True},
        {"name": "python_version", "ok": True, "value": sys.version.split()[0]},
        {"name": "platform", "ok": True, "value": sys.platform},
    ]
    return _emit(
        cmd="selftest",
        ok=True,
        exit_code=0,
        reason_code="CICD_CLI.OK.SELFTEST",
        checks=checks,
    )


def _ping() -> int:
    return _emit(
        cmd="ping",
        ok=True,
        exit_code=0,
        reason_code="CICD_CLI.OK.PING",
        pong=True,
    )


def _unknown(args: List[str]) -> int:
    return _emit(
        cmd="unknown",
        ok=False,
        exit_code=1,
        reason_code="CICD_CLI.FAIL.UNKNOWN_COMMAND",
        argv=args[:32],
    )


def _sanitize_args(argv: List[str]) -> List[str]:
    return [a.strip() for a in argv if a is not None]


def main(argv: List[str]) -> int:
    args = _sanitize_args(argv[1:])

    if len(args) == 0:
        return _help()

    cmd = args[0]

    if cmd in ("--help", "-h", "help"):
        return _help()
    if cmd in ("version", "--version", "-V"):
        return _version()
    if cmd == "selftest":
        return _selftest()
    if cmd == "ping":
        return _ping()

    return _unknown(args)


def _safe_entry(argv: List[str]) -> int:
    try:
        return main(argv)
    except SystemExit as e:
        code = getattr(e, "code", 2)
        try:
            ic = int(code) if code is not None else 2
        except Exception:
            ic = 2
        return _emit(
            cmd="infra",
            ok=False,
            exit_code=2 if ic not in (0, 1, 2) else ic,
            reason_code="CICD_CLI.INFRA.SYSTEM_EXIT",
            system_exit_code=str(code),
        )
    except Exception as e:
        return _emit(
            cmd="infra",
            ok=False,
            exit_code=2,
            reason_code="CICD_CLI.INFRA.EXCEPTION",
            err_type=type(e).__name__,
            err_msg=str(e)[:800],
        )


if __name__ == "__main__":
    raise SystemExit(_safe_entry(sys.argv))

