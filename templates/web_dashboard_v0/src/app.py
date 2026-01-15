from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

PRODUCT_ID = "web_dashboard_v0"
VERSION = os.environ.get("WEB_DASHBOARD_V0_VERSION", "0.1.0")

# IMPORTANT: this exact line is required by ensure_entrypoint_overlay()
STDOUT_MODE = "win_writefile_or_oswrite_v2"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _win_writefile_stdout(data: bytes) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE

        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL

        # -11 unsigned
        std_out = wintypes.DWORD(0xFFFFFFF5)
        h = kernel32.GetStdHandle(std_out)

        invalid = ctypes.c_void_p(-1).value
        if h is None or int(h) == 0 or int(h) == int(invalid):
            return False

        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(h, data, len(data), ctypes.byref(written), None)
        return bool(ok) and int(written.value) == len(data)
    except Exception:
        return False


def emit_one_json(payload: dict[str, Any]) -> None:
    # Contract: exactly 1 JSON line to stdout with newline.
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    data = line.encode("utf-8")

    # 1) Windows WriteFile (most robust for weird STDOUT handles)
    if STDOUT_MODE == "win_writefile_or_oswrite_v2" and _win_writefile_stdout(data):
        return

    # 2) os.write(fd=1)
    try:
        os.write(1, data)
        return
    except Exception:
        pass

    # 3) sys.stdout fallback
    sys.stdout.write(line)
    sys.stdout.flush()


def summary(
    schema: str,
    ok: bool,
    exit_code: int,
    reason_code: str,
    child_reason_code: str,
    **extra: Any,
) -> dict[str, Any]:
    rc = reason_code or "INFRA_REASON_NULL_FORBIDDEN"
    crc = child_reason_code or "INFRA_CHILD_REASON_NULL_FORBIDDEN"
    base: dict[str, Any] = {
        "schema": schema,
        "ok": bool(ok),
        "exit_code": int(exit_code),
        "reason_code": rc,
        "child_reason_code": crc,
        "ts_utc": utc_now_iso(),
        "product_id": PRODUCT_ID,
        "version": VERSION,
        "stdout_mode": STDOUT_MODE,
        "is_frozen": bool(getattr(sys, "frozen", False)),
        "exe_path": sys.executable,
    }
    base.update(extra)
    return base


class _State:
    def __init__(self) -> None:
        self._start_ts = time.time()
        self._ready = False
        self._lock = threading.Lock()

    def uptime_s(self) -> float:
        return round(time.time() - self._start_ts, 3)

    def set_ready(self, v: bool) -> None:
        with self._lock:
            self._ready = bool(v)

    def is_ready(self) -> bool:
        with self._lock:
            return bool(self._ready)


def make_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/health":
                self._send_json(
                    200,
                    {
                        "schema": "server_contract_v0.health",
                        "ok": True,
                        "ts_utc": utc_now_iso(),
                        "product_id": PRODUCT_ID,
                        "version": VERSION,
                        "uptime_s": state.uptime_s(),
                    },
                )
                return

            if path == "/ready":
                if state.is_ready():
                    self._send_json(
                        200,
                        {
                            "schema": "server_contract_v0.ready",
                            "ok": True,
                            "ts_utc": utc_now_iso(),
                            "product_id": PRODUCT_ID,
                            "version": VERSION,
                            "uptime_s": state.uptime_s(),
                        },
                    )
                    return

                self._send_json(
                    503,
                    {
                        "schema": "server_contract_v0.ready",
                        "ok": False,
                        "reason_code": "NOT_READY",
                        "ts_utc": utc_now_iso(),
                        "product_id": PRODUCT_ID,
                        "version": VERSION,
                        "uptime_s": state.uptime_s(),
                    },
                )
                return

            self._send_json(
                404,
                {
                    "schema": "server_contract_v0.http_404",
                    "ok": False,
                    "reason_code": "NOT_FOUND",
                    "ts_utc": utc_now_iso(),
                    "product_id": PRODUCT_ID,
                    "version": VERSION,
                },
            )

    return Handler


def cmd_version() -> int:
    emit_one_json(summary("server_contract_v0.version", True, 0, "OK", "OK"))
    return 0


def cmd_ping() -> int:
    emit_one_json(summary("server_contract_v0.ping", True, 0, "OK", "OK"))
    return 0


def cmd_selftest() -> int:
    tests: list[dict[str, Any]] = [
        {"name": "version_present", "ok": bool(VERSION)},
        {"name": "stdout_mode_present", "ok": bool(STDOUT_MODE)},
    ]
    ok_all = all(bool(t["ok"]) for t in tests)
    rc = 0 if ok_all else 1
    emit_one_json(
        summary(
            "server_contract_v0.selftest",
            ok_all,
            rc,
            "OK" if ok_all else "SELFTEST_FAIL",
            "OK" if ok_all else "SELFTEST_FAIL",
            tests=tests,
        )
    )
    return rc


def cmd_help() -> int:
    emit_one_json(
        summary(
            "server_contract_v0.help",
            True,
            0,
            "OK",
            "OK",
            commands=[
                "version",
                "selftest",
                "ping",
                "serve --host 127.0.0.1 --port 17811 --stop-flag stop.flag --ready-after-ms 200",
                "--help/-h/help",
            ],
            endpoints=["GET /health", "GET /ready"],
        )
    )
    return 0


def watch_stop_flag(stop_flag: str, httpd: ThreadingHTTPServer, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        if os.path.exists(stop_flag):
            break
        time.sleep(0.2)
    try:
        httpd.shutdown()
    except Exception:
        pass


def cmd_serve(host: str, port: int, stop_flag: str, ready_after_ms: int) -> int:
    if not stop_flag:
        emit_one_json(summary("server_contract_v0.startup", False, 1, "FAIL_BAD_ARGS", "FAIL_BAD_ARGS", detail="STOP_FLAG_REQUIRED"))
        return 1
    if port <= 0 or port > 65535:
        emit_one_json(summary("server_contract_v0.startup", False, 1, "FAIL_BAD_ARGS", "FAIL_BAD_ARGS", detail="PORT_RANGE"))
        return 1

    state = _State()
    handler = make_handler(state)

    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as e:
        winerror = getattr(e, "winerror", None)
        if winerror == 10048:
            emit_one_json(summary("server_contract_v0.startup", False, 2, "INFRA_BIND_FAILED", "INFRA_BIND_FAILED", host=host, port=port))
            return 2
        emit_one_json(
            summary(
                "server_contract_v0.startup",
                False,
                2,
                "INFRA_BIND_ERROR",
                "INFRA_BIND_ERROR",
                host=host,
                port=port,
                err=str(e),
                winerror=winerror,
            )
        )
        return 2
    except Exception as e:
        emit_one_json(summary("server_contract_v0.startup", False, 2, "INFRA_BIND_ERROR", "INFRA_BIND_ERROR", host=host, port=port, err=str(e)))
        return 2

    stop_event = threading.Event()

    def ready_worker() -> None:
        if ready_after_ms > 0:
            time.sleep(max(0, ready_after_ms) / 1000.0)
        state.set_ready(True)

    threading.Thread(target=ready_worker, daemon=True).start()
    threading.Thread(target=watch_stop_flag, args=(stop_flag, httpd, stop_event), daemon=True).start()

    def shutdown_now() -> None:
        if stop_event.is_set():
            return
        stop_event.set()
        try:
            httpd.shutdown()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, lambda *_: shutdown_now())
        signal.signal(signal.SIGTERM, lambda *_: shutdown_now())
    except Exception:
        pass

    # Exactly one JSON line on startup
    emit_one_json(
        summary(
            "server_contract_v0.startup",
            True,
            0,
            "OK",
            "OK",
            pid=os.getpid(),
            host=host,
            port=port,
            stop_flag=stop_flag,
            urls={"health": f"http://{host}:{port}/health", "ready": f"http://{host}:{port}/ready"},
        )
    )

    try:
        httpd.serve_forever(poll_interval=0.2)
        return 0
    except KeyboardInterrupt:
        shutdown_now()
        return 0
    except Exception:
        return 2
    finally:
        stop_event.set()
        try:
            httpd.server_close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if not args or "--help" in args or "-h" in args or "help" in args:
        return cmd_help()

    cmd = args[0].strip().lower()

    if cmd == "version":
        return cmd_version()
    if cmd == "selftest":
        return cmd_selftest()
    if cmd == "ping":
        return cmd_ping()

    if cmd == "serve":
        host = "127.0.0.1"
        port = 17811
        stop_flag = ""
        ready_after_ms = 0

        i = 1
        while i < len(args):
            a = args[i]
            if a == "--host" and i + 1 < len(args):
                host = args[i + 1]
                i += 2
                continue
            if a == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
                continue
            if a == "--stop-flag" and i + 1 < len(args):
                stop_flag = args[i + 1]
                i += 2
                continue
            if a == "--ready-after-ms" and i + 1 < len(args):
                ready_after_ms = int(args[i + 1])
                i += 2
                continue

            emit_one_json(
                summary(
                    "server_contract_v0.cli",
                    False,
                    1,
                    "FAIL_BAD_ARGS",
                    "FAIL_BAD_ARGS",
                    detail="UNKNOWN_FLAG",
                    flag=a,
                )
            )
            return 1

        return cmd_serve(host, port, stop_flag, ready_after_ms)

    emit_one_json(
        summary(
            "server_contract_v0.cli",
            False,
            1,
            "FAIL_BAD_ARGS",
            "FAIL_BAD_ARGS",
            detail="UNKNOWN_COMMAND",
            command=cmd,
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
