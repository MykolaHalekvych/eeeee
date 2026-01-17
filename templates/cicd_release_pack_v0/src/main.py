import json
import sys
from datetime import datetime, timezone

__VERSION__ = "0.1.0"


def _ts_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(payload: dict, rc: int) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return rc


def _help() -> int:
    return _emit(
        {
            "schema": "cicd_release_pack_v0_cli",
            "ok": True,
            "cmd": "help",
            "ts_utc": _ts_utc(),
            "usage": [
                "app.exe --help",
                "app.exe version",
                "app.exe selftest",
                "app.exe ping",
                "app.exe <unknown>  (returns rc=1 + JSON)",
            ],
            "commands": ["--help", "-h", "help", "version", "selftest", "ping"],
        },
        0,
    )


def _version() -> int:
    return _emit(
        {
            "schema": "cicd_release_pack_v0_cli",
            "ok": True,
            "cmd": "version",
            "ts_utc": _ts_utc(),
            "version": __VERSION__,
        },
        0,
    )


def _selftest() -> int:
    # Минимальный selftest: CLI отвечает JSON и возвращает rc=0
    return _emit(
        {
            "schema": "cicd_release_pack_v0_cli",
            "ok": True,
            "cmd": "selftest",
            "ts_utc": _ts_utc(),
            "checks": [{"name": "cli_json_stdout", "ok": True}],
        },
        0,
    )


def _ping() -> int:
    return _emit(
        {
            "schema": "cicd_release_pack_v0_cli",
            "ok": True,
            "cmd": "ping",
            "ts_utc": _ts_utc(),
            "pong": True,
        },
        0,
    )


def main(argv: list[str]) -> int:
    # argv includes program name
    args = argv[1:]

    if len(args) == 0:
        return _help()

    cmd = args[0].strip()

    if cmd in ("--help", "-h", "help"):
        return _help()
    if cmd == "version":
        return _version()
    if cmd == "selftest":
        return _selftest()
    if cmd == "ping":
        return _ping()

    return _emit(
        {
            "schema": "cicd_release_pack_v0_cli",
            "ok": False,
            "cmd": "unknown",
            "ts_utc": _ts_utc(),
            "reason": "UNKNOWN_COMMAND",
            "argv": args,
        },
        1,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
