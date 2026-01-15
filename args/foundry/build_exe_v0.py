# args/foundry/build_exe_v0.py
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = "build_exe_v0"

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_reason(x: Optional[str], fallback: str) -> str:
    x = (x or "").strip()
    return x if x else fallback


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8", newline="\n")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
        newline="\n",
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def step_paths(out_dir: Path, step_id: str, name: str) -> Tuple[Path, Path, Path]:
    base = f"step_{step_id}_{name}"
    return (
        out_dir / f"{base}.stdout.txt",
        out_dir / f"{base}.stderr.txt",
        out_dir / f"{base}.summary.json",
    )


def compute_pre_out_dir(args: argparse.Namespace) -> Optional[Path]:
    """
    Precompute out_dir BEFORE main_inner() so exception handlers write into the SAME out_dir
    that holds step_* artifacts.
    """
    try:
        repo = Path(args.repo).resolve()

        if getattr(args, "out_dir", None):
            return Path(args.out_dir).resolve()

        if getattr(args, "workspace", None):
            pid = getattr(args, "product_id", None) or "workspace_build"
            return (repo / "dist" / pid).resolve()

        pid = getattr(args, "product_id", None)
        if not pid:
            return None
        return (repo / "dist" / pid).resolve()
    except Exception:
        return None


def write_exception_step(out_dir: Path, exc: Exception, code: int) -> Dict[str, Any]:
    import traceback

    so, se, ss = step_paths(out_dir, "99", "exception")
    write_text(so, "")
    write_text(se, traceback.format_exc())

    s = {
        "id": "99_exception",
        "ok": False,
        "exit_code": int(code),
        "reason_code": "FAIL_EXCEPTION",
        "child_reason_code": safe_reason(str(exc), "INFRA_EXCEPTION"),
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "error": {"kind": exc.__class__.__name__, "message": str(exc)},
    }
    write_json(ss, s)
    return s


def run_cmd(cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "cmd": cmd,
        "cwd": str(cwd) if cwd else None,
        "rc": int(p.returncode),
        "stdout": p.stdout or "",
        "stderr": p.stderr or "",
    }


def emit_json_stdout(payload: Dict[str, Any]) -> None:
    # Contract: exactly 1 JSON line to stdout with newline.
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def classify_exit_code(exc: Exception) -> int:
    msg = str(exc)
    infra_markers = [
        "PyInstaller is not available",
        "No module named",
        "pip",
        "ruff",
    ]
    if any(m in msg for m in infra_markers):
        return RC_INFRA
    if isinstance(exc, (FileNotFoundError, ValueError, RuntimeError)):
        return RC_FAIL
    return RC_INFRA


class _ArgParser(argparse.ArgumentParser):
    def print_help(self, file=None):  # noqa: ANN001
        raise ValueError("BAD_ARGS_HELP")

    def print_usage(self, file=None):  # noqa: ANN001
        raise ValueError("BAD_ARGS_USAGE")

    def error(self, message: str):  # noqa: ARG002
        raise ValueError("BAD_ARGS_PARSE_ERROR")

    def exit(self, status: int = 0, message: Optional[str] = None):  # noqa: ARG002
        raise ValueError("BAD_ARGS_EXIT")


@dataclass
class Product:
    product_id: str
    version: str
    template_dir: str
    entrypoint: str
    exe_name: str


def load_product(repo: Path, product_id: str) -> Product:
    manifest_path = repo / "manifests" / "products" / f"{product_id}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"product manifest not found: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if data.get("product_id") != product_id:
        raise ValueError("product_id mismatch in manifest")
    return Product(
        product_id=product_id,
        version=str(data.get("version", "0.0.0")),
        template_dir=str(data["template_dir"]),
        entrypoint=str(data["entrypoint"]),
        exe_name=str(data.get("exe_name", "app.exe")),
    )


def py_compile_tree(root: Path) -> Dict[str, Any]:
    """
    RGLOB-safe py_compile tree: any rglob failure becomes an error record (no hidden crash).
    """
    import py_compile

    errors: List[Dict[str, str]] = []
    try:
        py_files = sorted(root.rglob("*.py"))
    except Exception as e:  # noqa: BLE001
        return {
            "files": [],
            "errors": [{"file": str(root), "error": "RGLOB_ERROR:" + repr(e)}],
            "ok": False,
        }

    for f in py_files:
        try:
            py_compile.compile(str(f), doraise=True)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": str(f), "error": repr(e)})

    return {"files": [str(p) for p in py_files], "errors": errors, "ok": len(errors) == 0}


def build_runbook(exe_name: str, is_server: bool) -> str:
    if is_server:
        return f"""# Runbook (Release Pack v0) — Server Contract v0

## Commands (JSON stdout)

- Version:
  - `{exe_name} version`

- Selftest:
  - `{exe_name} selftest`

- Ping:
  - `{exe_name} ping`

- Serve:
  - `{exe_name} serve --host 127.0.0.1 --port 17811 --stop-flag stop.flag --ready-after-ms 200`

## Endpoints

- Health: `GET /health`
- Ready:  `GET /ready` (200 when ready, else 503)

## Shutdown

- Create stop flag file:
  - `type nul > stop.flag`
"""
    return f"""# Runbook (Release Pack v0)

## Run

- Show help:
  - `{exe_name} --help`

- Version:
  - `{exe_name} version`

- Ping:
  - `{exe_name} ping`
"""


def build_evidence_md(
    inputs: dict[str, Any],
    checks: dict[str, Any],
    toolchain: dict[str, Any],
    pyinstaller_cmd: list[str],
    pyinstaller_res: dict[str, Any],
    postcheck: dict[str, Any],
) -> str:
    def fence(s: str) -> str:
        return "```\n" + (s or "").rstrip() + "\n```\n"

    md: list[str] = []
    md.append("# Evidence (EXE Pack v0)\n")

    md.append("\n## Inputs\n")
    md.append(fence(json.dumps(inputs, indent=2, ensure_ascii=False)))

    md.append("\n## Toolchain\n")
    md.append(fence(json.dumps(toolchain, indent=2, ensure_ascii=False)))

    md.append("\n## Preflight checks\n")
    md.append("### py_compile\n")
    md.append(fence(json.dumps(checks.get("py_compile"), indent=2, ensure_ascii=False)))
    md.append("### ruff\n")
    md.append(fence(json.dumps(checks.get("ruff"), indent=2, ensure_ascii=False)))
    md.append("### python smoke\n")
    md.append(fence(json.dumps(checks.get("smoke"), indent=2, ensure_ascii=False)))

    md.append("\n## Build (PyInstaller)\n")
    md.append("### command\n")
    md.append(fence(" ".join(pyinstaller_cmd)))
    md.append("### result\n")
    md.append(fence(json.dumps(pyinstaller_res, indent=2, ensure_ascii=False)))

    md.append("\n## Post-build check\n")
    md.append(fence(json.dumps(postcheck, indent=2, ensure_ascii=False)))

    return "".join(md)


# Fallback Server Contract v0 app.py (non-bypass)
# IMPORTANT: must be RAW string; otherwise sequences like "\n" inside code become real newlines
# and break the generated app.py (unterminated string literal).
FALLBACK_WEB_DASHBOARD_APP_PY = r"""
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
"""


def ensure_entrypoint_overlay(product_id: str, entry_script: Path) -> Dict[str, Any]:
    marker = "server_contract_v0"
    placeholder = "Placeholder dashboard entrypoint"
    required = 'STDOUT_MODE = "win_writefile_or_oswrite_v2"'

    try:
        current = entry_script.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason_code": "INFRA_ENTRY_READ_FAILED", "error": repr(e), "entrypoint": str(entry_script)}

    if product_id != "web_dashboard_v0":
        return {"ok": True, "reason_code": "OK", "action": "skip_not_target", "entrypoint": str(entry_script)}

    if (marker in current) and (placeholder not in current) and (required in current):
        return {"ok": True, "reason_code": "OK", "action": "already_server_contract", "entrypoint": str(entry_script)}

    if (marker not in FALLBACK_WEB_DASHBOARD_APP_PY) or (required not in FALLBACK_WEB_DASHBOARD_APP_PY):
        return {"ok": False, "reason_code": "INFRA_FALLBACK_BAD", "entrypoint": str(entry_script)}

    try:
        entry_script.write_text(FALLBACK_WEB_DASHBOARD_APP_PY, encoding="utf-8", newline="\n")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason_code": "INFRA_ENTRY_WRITE_FAILED", "error": repr(e), "entrypoint": str(entry_script)}

    return {"ok": True, "reason_code": "OK", "action": "overwrote_from_fallback", "entrypoint": str(entry_script)}


def main_inner(args: argparse.Namespace) -> Tuple[Dict[str, Any], int, Path]:
    repo = Path(args.repo).resolve()

    if args.workspace is None:
        if not args.product_id:
            raise ValueError("BAD_ARGS_MISSING_PRODUCT_ID_OR_WORKSPACE")
        product = load_product(repo, args.product_id)
        product_id = product.product_id
        version = product.version
        src_root = (repo / product.template_dir).resolve()
        entry_script = (src_root / product.entrypoint).resolve()
        exe_name = product.exe_name
        config_src = src_root / "config.example.json"
        inputs: Dict[str, Any] = {
            "mode": "product_id",
            "product_id": product_id,
            "template_root": str(src_root),
            "entrypoint": str(entry_script),
        }
    else:
        ws = Path(args.workspace).resolve()
        if not ws.exists():
            raise FileNotFoundError(f"workspace not found: {ws}")
        if not args.entrypoint:
            raise ValueError("BAD_ARGS_WORKSPACE_REQUIRES_ENTRYPOINT")

        rel_ep = Path(args.entrypoint)
        entry_script = (ws / rel_ep).resolve()
        entry_script.relative_to(ws)
        exe_name = "app.exe"
        product_id = args.product_id or "workspace_build"
        version = "0.0.0"
        config_src = ws / "config.example.json"
        if not config_src.exists():
            raise FileNotFoundError(f"config.example.json not found in workspace: {config_src}")
        src_root = ws
        inputs = {
            "mode": "workspace",
            "product_id": product_id,
            "workspace": str(ws),
            "entrypoint": str(entry_script),
        }

    if not entry_script.exists():
        raise FileNotFoundError(f"entrypoint not found: {entry_script}")

    out_dir = Path(args.out_dir).resolve() if args.out_dir else (repo / "dist" / product_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    steps: List[Dict[str, Any]] = []
    last_child_reason = "OK"

    def add_step(s: Dict[str, Any]) -> None:
        nonlocal last_child_reason
        s["child_reason_code"] = safe_reason(s.get("child_reason_code"), "INFRA_CHILD_REASON_MISSING")
        steps.append(s)
        last_child_reason = s["child_reason_code"]

    # STEP 00: entrypoint overlay (non-bypass)
    so, se, ss = step_paths(out_dir, "00", "entrypoint_overlay")
    write_text(so, "")
    write_text(se, "")
    overlay = ensure_entrypoint_overlay(product_id, entry_script)
    inputs["entrypoint_overlay"] = overlay
    s0 = {
        "id": "00_entrypoint_overlay",
        "ok": bool(overlay.get("ok") is True),
        "exit_code": 0 if overlay.get("ok") is True else 1,
        "reason_code": "OK" if overlay.get("ok") is True else "FAIL_ENTRYPOINT_OVERLAY",
        "child_reason_code": safe_reason(str(overlay.get("reason_code")), "FAIL_ENTRYPOINT_OVERLAY"),
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "overlay": overlay,
    }
    write_json(ss, s0)
    add_step(s0)
    if not s0["ok"]:
        raise RuntimeError(f"entrypoint overlay failed: {overlay.get('reason_code')}")

    # STEP 01: inputs
    so, se, ss = step_paths(out_dir, "01", "inputs")
    write_text(so, "")
    write_text(se, "")
    s1 = {
        "id": "01_inputs",
        "ok": True,
        "exit_code": 0,
        "reason_code": "OK",
        "child_reason_code": "OK",
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "inputs": inputs,
    }
    write_json(ss, s1)
    add_step(s1)

    # STEP 02: py_compile
    so, se, ss = step_paths(out_dir, "02", "py_compile")
    pc = py_compile_tree(src_root)
    write_text(so, "")
    write_text(se, "")
    s2 = {
        "id": "02_py_compile",
        "ok": bool(pc.get("ok") is True),
        "exit_code": 0 if pc.get("ok") is True else 1,
        "reason_code": "OK" if pc.get("ok") is True else "FAIL_PY_COMPILE",
        "child_reason_code": "OK" if pc.get("ok") is True else "FAIL_PY_COMPILE",
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "py_compile": pc,
    }
    write_json(ss, s2)
    add_step(s2)
    if not s2["ok"]:
        raise RuntimeError("FAIL_PY_COMPILE")

    # STEP 03: ruff
    so, se, ss = step_paths(out_dir, "03", "ruff")
    ruff = run_cmd([sys.executable, "-m", "ruff", "check", str(src_root)])
    write_text(so, ruff.get("stdout", ""))
    write_text(se, ruff.get("stderr", ""))
    s3 = {
        "id": "03_ruff",
        "ok": ruff["rc"] == 0,
        "exit_code": 0 if ruff["rc"] == 0 else 1,
        "reason_code": "OK" if ruff["rc"] == 0 else "FAIL_RUFF",
        "child_reason_code": "OK" if ruff["rc"] == 0 else "FAIL_RUFF_RC",
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "ruff": ruff,
    }
    write_json(ss, s3)
    add_step(s3)
    if not s3["ok"]:
        raise RuntimeError("FAIL_RUFF")

    # STEP 04: python smoke
    so, se, ss = step_paths(out_dir, "04", "python_smoke")
    env = os.environ.copy()
    pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(src_root) if not pp else (str(src_root) + os.pathsep + pp)

    smoke_help = run_cmd([sys.executable, str(entry_script), "--help"], cwd=src_root, env=env)
    smoke_ver: Optional[Dict[str, Any]] = None
    chosen = "help"
    ok = smoke_help["rc"] == 0
    if not ok:
        smoke_ver = run_cmd([sys.executable, str(entry_script), "version"], cwd=src_root, env=env)
        chosen = "version"
        ok = smoke_ver["rc"] == 0

    smoke_obj = {
        "chosen": chosen,
        "help": smoke_help,
        "version": smoke_ver,
        "cwd": str(src_root),
        "py_path": env["PYTHONPATH"],
    }
    write_text(so, (smoke_help.get("stdout", "") or ""))
    write_text(se, (smoke_help.get("stderr", "") or ""))
    s4 = {
        "id": "04_python_smoke",
        "ok": ok,
        "exit_code": 0 if ok else 1,
        "reason_code": "OK" if ok else "FAIL_PYTHON_SMOKE",
        "child_reason_code": "OK" if ok else "FAIL_PYTHON_SMOKE_RC",
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "smoke": smoke_obj,
    }
    write_json(ss, s4)
    add_step(s4)
    if not s4["ok"]:
        raise RuntimeError("FAIL_PYTHON_SMOKE")

    # STEP 05: toolchain
    so, se, ss = step_paths(out_dir, "05", "toolchain")
    toolchain = {
        "python": sys.version.replace("\n", " "),
        "python_exe": sys.executable,
        "platform": platform.platform(),
        "pip": run_cmd([sys.executable, "-m", "pip", "--version"]),
        "ruff": run_cmd([sys.executable, "-m", "ruff", "--version"]),
        "pyinstaller": run_cmd([sys.executable, "-m", "PyInstaller", "--version"]),
    }
    write_text(so, json.dumps(toolchain, ensure_ascii=False, indent=2))
    write_text(se, "")
    ok_tc = toolchain["pyinstaller"]["rc"] == 0
    s5 = {
        "id": "05_toolchain",
        "ok": ok_tc,
        "exit_code": 0 if ok_tc else 2,
        "reason_code": "OK" if ok_tc else "INFRA_PYINSTALLER_MISSING",
        "child_reason_code": "OK" if ok_tc else "INFRA_PYINSTALLER_MISSING",
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "toolchain": toolchain,
    }
    write_json(ss, s5)
    add_step(s5)
    if not s5["ok"]:
        raise RuntimeError("PyInstaller is not available. Run scripts/bootstrap_tools_v1.ps1 first.")

    exe_base = exe_name[:-4] if exe_name.lower().endswith(".exe") else exe_name
    exe_path = out_dir / exe_name

    try:
        if exe_path.exists():
            exe_path.unlink()
    except Exception:
        pass

    # STEP 06: PyInstaller (FORCE --console)
    build_root = repo / "dist" / "_pyi_build"
    build_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work_dir = build_root / f"{product_id}__{stamp}__work"
    spec_dir = build_root / f"{product_id}__{stamp}__spec"

    pyinstaller_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--console",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        exe_base,
        "--distpath",
        str(out_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--paths",
        str(src_root),
        "--paths",
        str(entry_script.parent),
        str(entry_script),
    ]

    so, se, ss = step_paths(out_dir, "06", "pyinstaller")
    pyinstaller_res = run_cmd(pyinstaller_cmd, cwd=src_root, env=env)
    write_text(so, pyinstaller_res.get("stdout", ""))
    write_text(se, pyinstaller_res.get("stderr", ""))
    s6 = {
        "id": "06_pyinstaller",
        "ok": pyinstaller_res["rc"] == 0,
        "exit_code": 0 if pyinstaller_res["rc"] == 0 else 1,
        "reason_code": "OK" if pyinstaller_res["rc"] == 0 else "FAIL_PYINSTALLER",
        "child_reason_code": "OK" if pyinstaller_res["rc"] == 0 else "FAIL_PYINSTALLER_RC",
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "cmd": pyinstaller_cmd,
        "pyinstaller": pyinstaller_res,
    }
    write_json(ss, s6)
    add_step(s6)
    if not s6["ok"]:
        raise RuntimeError("FAIL_PYINSTALLER")

    candidate = out_dir / (exe_base + ".exe")
    if candidate.exists() and exe_path != candidate:
        try:
            if exe_path.exists():
                exe_path.unlink()
        except Exception:
            pass
        candidate.replace(exe_path)

    if not exe_path.exists():
        raise FileNotFoundError(f"app.exe not found after build: {exe_path}")

    # Copy config
    config_dst = out_dir / "config.example.json"
    if config_src.exists():
        shutil.copyfile(str(config_src), str(config_dst))
    else:
        write_text(config_dst, "{}\n")

    # STEP 07: postcheck (MUST have non-empty stdout for web_dashboard_v0 version)
    so, se, ss = step_paths(out_dir, "07", "postcheck")
    post_help = run_cmd([str(exe_path), "--help"], cwd=out_dir)
    post_ver = run_cmd([str(exe_path), "version"], cwd=out_dir)
    write_text(so, (post_help.get("stdout", "") or "") + "\n---\n" + (post_ver.get("stdout", "") or ""))
    write_text(se, (post_help.get("stderr", "") or "") + "\n---\n" + (post_ver.get("stderr", "") or ""))

    ok_help = post_help["rc"] == 0
    ok_ver_stdout = True
    ver_reason = "OK"
    if product_id == "web_dashboard_v0":
        ok_ver_stdout = (post_ver["rc"] == 0) and (len((post_ver.get("stdout") or "").strip()) > 0)
        if not ok_ver_stdout:
            ver_reason = "FAIL_POSTCHECK_VERSION_STDOUT_EMPTY"

    ok_post = ok_help and ok_ver_stdout
    s7 = {
        "id": "07_postcheck",
        "ok": ok_post,
        "exit_code": 0 if ok_post else 1,
        "reason_code": "OK" if ok_post else "FAIL_POSTCHECK",
        "child_reason_code": "OK" if ok_post else (ver_reason if not ok_ver_stdout else "FAIL_POSTCHECK_HELP"),
        "ts_utc": utc_ts(),
        "stdout_path": str(so),
        "stderr_path": str(se),
        "summary_path": str(ss),
        "postcheck": {"help": post_help, "version": post_ver},
    }
    write_json(ss, s7)
    add_step(s7)
    if not s7["ok"]:
        raise RuntimeError(s7["child_reason_code"])

    # STEP 08: artifacts
    is_server = product_id == "web_dashboard_v0"
    runbook_path = out_dir / "runbook.md"
    evidence_path = out_dir / "evidence.md"
    hashes_path = out_dir / "hashes.json"

    write_text(runbook_path, build_runbook(exe_name, is_server))

    checks_bundle = {
        "py_compile": pc,
        "ruff": ruff,
        "smoke": smoke_obj,
    }

    evidence_md = build_evidence_md(
        inputs=inputs,
        checks=checks_bundle,
        toolchain=toolchain,
        pyinstaller_cmd=pyinstaller_cmd,
        pyinstaller_res=pyinstaller_res,
        postcheck={"help": post_help, "version": post_ver},
    )
    write_text(evidence_path, evidence_md)

    hashes = {
        "schema": "hashes_v0",
        "product_id": product_id,
        "ts_utc": utc_ts(),
        "files": {
            exe_name: sha256_file(exe_path),
            "config.example.json": sha256_file(config_dst),
            "runbook.md": sha256_file(runbook_path),
            "evidence.md": sha256_file(evidence_path),
            "hashes.json": sha256_file(hashes_path),
        },
    }
    write_text(hashes_path, json.dumps(hashes, indent=2, ensure_ascii=False) + "\n")

    artifacts = {
        "app_exe": str(exe_path),
        "config_example": str(config_dst),
        "runbook": str(runbook_path),
        "evidence": str(evidence_path),
        "hashes": str(hashes_path),
    }

    payload = {
        "schema": SCHEMA,
        "ts_utc": utc_ts(),
        "ok": True,
        "exit_code": RC_OK,
        "reason_code": "OK",
        "child_reason_code": safe_reason(last_child_reason, "OK"),
        "product_id": product_id,
        "version": version,
        "out_dir": str(out_dir),
        "inputs": inputs,
        "steps": steps,
        "artifacts": artifacts,
    }
    return payload, RC_OK, out_dir


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    ap = _ArgParser(description="Build Windows portable EXE for a product (EXE Pack v0)", add_help=False)
    ap.add_argument("--repo", default=".", help="Repo root")
    ap.add_argument("--product-id", required=False)
    ap.add_argument("--out-dir", default=None, help="Override dist/<product_id>")
    ap.add_argument("--workspace", default=None, help="Build directly from a workspace directory")
    ap.add_argument("--entrypoint", default=None, help="Entrypoint relative to workspace (e.g., src/main.py)")

    out_dir: Optional[Path] = None

    try:
        args = ap.parse_args(argv)

        out_dir = compute_pre_out_dir(args)
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)

        payload, code, actual_out_dir = main_inner(args)
        out_dir = actual_out_dir

        write_json(Path(payload["out_dir"]) / "summary.json", payload)
        emit_json_stdout(payload)
        return int(code)

    except KeyboardInterrupt as e:
        code = RC_INFRA
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if out_dir is None:
            out_dir = (Path(".").resolve() / "dist" / "_build_exe_v0_error" / ts)
            out_dir.mkdir(parents=True, exist_ok=True)

        _ = write_exception_step(out_dir, e, code)

        payload = {
            "schema": SCHEMA,
            "ts_utc": utc_ts(),
            "ok": False,
            "exit_code": int(code),
            "reason_code": "FAIL",
            "child_reason_code": "INFRA_KEYBOARD_INTERRUPT",
            "out_dir": str(out_dir),
        }
        write_json(out_dir / "summary.json", payload)
        emit_json_stdout(payload)
        return int(code)

    except Exception as e:  # noqa: BLE001
        code = classify_exit_code(e)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if out_dir is None:
            out_dir = (Path(".").resolve() / "dist" / "_build_exe_v0_error" / ts)
            out_dir.mkdir(parents=True, exist_ok=True)

        _ = write_exception_step(out_dir, e, code)

        payload = {
            "schema": SCHEMA,
            "ts_utc": utc_ts(),
            "ok": False,
            "exit_code": int(code),
            "reason_code": "FAIL",
            "child_reason_code": safe_reason(str(e), "INFRA_EXCEPTION"),
            "out_dir": str(out_dir),
            "error": {"kind": e.__class__.__name__, "message": str(e)},
        }
        write_json(out_dir / "summary.json", payload)
        emit_json_stdout(payload)
        return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
