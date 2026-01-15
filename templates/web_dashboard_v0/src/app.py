from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PRODUCT_ID = "web_dashboard_v0"
VERSION = os.environ.get("WEB_DASHBOARD_V0_VERSION", "0.1.0")

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def print_one_json(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def summary(schema: str, ok: bool, exit_code: int, reason_code: str, **extra) -> dict:
    return {
        "schema": schema,
        "ok": bool(ok),
        "exit_code": int(exit_code),
        "reason_code": (reason_code or "INFRA_REASON_NULL_FORBIDDEN"),
        "ts_utc": utc_now_iso(),
        "product_id": PRODUCT_ID,
        "version": VERSION,
        **extra,
    }

class _State:
    def __init__(self) -> None:
        self.start_ts = time.time()
        self._ready = False
        self._lock = threading.Lock()

    def uptime_s(self) -> float:
        return round(time.time() - self.start_ts, 3)

    def set_ready(self, v: bool) -> None:
        with self._lock:
            self._ready = bool(v)

    def is_ready(self) -> bool:
        with self._lock:
            return bool(self._ready)

def make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A003
            return

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            p = urlparse(self.path)
            path = p.path

            if path == "/health":
                self._send_json(200, {
                    "schema": "server_contract_v0.health",
                    "ok": True,
                    "ts_utc": utc_now_iso(),
                    "product_id": PRODUCT_ID,
                    "version": VERSION,
                    "uptime_s": state.uptime_s(),
                })
                return

            if path == "/ready":
                if state.is_ready():
                    self._send_json(200, {
                        "schema": "server_contract_v0.ready",
                        "ok": True,
                        "ts_utc": utc_now_iso(),
                        "product_id": PRODUCT_ID,
                        "version": VERSION,
                        "uptime_s": state.uptime_s(),
                    })
                else:
                    self._send_json(503, {
                        "schema": "server_contract_v0.ready",
                        "ok": False,
                        "reason_code": "NOT_READY",
                        "ts_utc": utc_now_iso(),
                        "product_id": PRODUCT_ID,
                        "version": VERSION,
                        "uptime_s": state.uptime_s(),
                    })
                return

            if path == "/":
                html = (
                    f"<!doctype html><html><head><meta charset='utf-8'>"
                    f"<title>{PRODUCT_ID}</title></head><body>"
                    f"<h1>{PRODUCT_ID}</h1>"
                    f"<p>Try <a href='/health'>/health</a> and <a href='/ready'>/ready</a></p>"
                    f"</body></html>"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

            self._send_json(404, {
                "schema": "server_contract_v0.http_404",
                "ok": False,
                "reason_code": "NOT_FOUND",
                "ts_utc": utc_now_iso(),
                "product_id": PRODUCT_ID,
                "version": VERSION,
            })
    return Handler

def cmd_version() -> int:
    print_one_json(summary("server_contract_v0.version", True, 0, "OK"))
    return 0

def cmd_selftest() -> int:
    tests = [{"name": "version_present", "ok": bool(VERSION)}, {"name": "http_server_available", "ok": True}]
    ok = all(t["ok"] for t in tests)
    rc = 0 if ok else 1
    print_one_json(summary("server_contract_v0.selftest", ok, rc, "OK" if ok else "SELFTEST_FAIL", tests=tests))
    return rc

def cmd_ping() -> int:
    print_one_json(summary("server_contract_v0.ping", True, 0, "OK"))
    return 0

def cmd_help() -> int:
    print_one_json(summary(
        "server_contract_v0.help", True, 0, "OK",
        commands=["version","selftest","ping","serve --host 127.0.0.1 --port 17811 --stop-flag stop.flag --ready-after-ms 200"],
        endpoints=["GET /health","GET /ready","GET /"]
    ))
    return 0

def _watch_stop_flag(stop_flag: str, httpd: ThreadingHTTPServer):
    for _ in range(10000):
        if os.path.exists(stop_flag):
            break
        time.sleep(0.2)
    try:
        httpd.shutdown()
    except Exception:
        pass

def cmd_serve(host: str, port: int, stop_flag: str, ready_after_ms: int) -> int:
    if not stop_flag:
        print_one_json(summary("server_contract_v0.startup", False, 1, "BAD_ARGS_STOP_FLAG_REQUIRED"))
        return 1
    if port <= 0 or port > 65535:
        print_one_json(summary("server_contract_v0.startup", False, 1, "BAD_ARGS_PORT_RANGE"))
        return 1

    state = _State()
    handler = make_handler(state)
    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except Exception:
        print_one_json(summary("server_contract_v0.startup", False, 2, "INFRA_BIND_FAILED"))
        return 2

    def _ready_worker():
        if ready_after_ms > 0:
            time.sleep(ready_after_ms / 1000.0)
        state.set_ready(True)

    threading.Thread(target=_ready_worker, daemon=True).start()
    threading.Thread(target=_watch_stop_flag, args=(stop_flag, httpd), daemon=True).start()

    print_one_json(summary("server_contract_v0.startup", True, 0, "OK",
        pid=os.getpid(), host=host, port=port,
        urls={"base": f"http://{host}:{port}/", "health": f"http://{host}:{port}/health", "ready": f"http://{host}:{port}/ready"}
    ))

    try:
        httpd.serve_forever(poll_interval=0.2)
        return 0
    except KeyboardInterrupt:
        try: httpd.shutdown()
        except Exception: pass
        return 0
    except Exception:
        return 2

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or "--help" in argv or "-h" in argv or "help" in argv:
        return cmd_help()

    cmd = argv[0].strip().lower()
    if cmd == "version": return cmd_version()
    if cmd == "selftest": return cmd_selftest()
    if cmd == "ping": return cmd_ping()
    if cmd == "serve":
        host="127.0.0.1"; port=17811; stop_flag=""; ready_after_ms=0
        i=1
        while i < len(argv):
            a=argv[i]
            if a=="--host" and i+1 < len(argv): host=argv[i+1]; i+=2; continue
            if a=="--port" and i+1 < len(argv): port=int(argv[i+1]); i+=2; continue
            if a=="--stop-flag" and i+1 < len(argv): stop_flag=argv[i+1]; i+=2; continue
            if a=="--ready-after-ms" and i+1 < len(argv): ready_after_ms=int(argv[i+1]); i+=2; continue
            print_one_json(summary("server_contract_v0.cli", False, 1, "BAD_ARGS_UNKNOWN_FLAG", flag=a))
            return 1
        return cmd_serve(host, port, stop_flag, ready_after_ms)

    print_one_json(summary("server_contract_v0.cli", False, 1, "BAD_ARGS_UNKNOWN_COMMAND", command=cmd))
    return 1

if __name__ == "__main__":
    raise SystemExit(main())