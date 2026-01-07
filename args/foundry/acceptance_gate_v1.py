from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCHEMA = "acceptance_gate_v1"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def emit(payload: Dict[str, Any], code: int) -> int:
    payload["schema"] = payload.get("schema", SCHEMA)
    payload["ts_utc"] = payload.get("ts_utc", utc_ts())
    payload["exit_code"] = int(code)
    payload["ok"] = (int(code) == 0)
    s = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write(s)
    sys.stdout.flush()
    return int(code)


def run(cmd: List[str], cwd: Path | None = None) -> Dict[str, Any]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    return {"cmd": cmd, "rc": int(p.returncode), "stdout": p.stdout or "", "stderr": p.stderr or ""}


def read_text_safe(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def extract_zip(zip_path: Path, dst: Path) -> None:
    with zipfile.ZipFile(str(zip_path), "r") as z:
        z.extractall(str(dst))


def require_files(root: Path, files: List[str]) -> Tuple[bool, List[str]]:
    missing = []
    for f in files:
        if not (root / f).exists():
            missing.append(f)
    return (len(missing) == 0), missing


def evidence_has_sections(evidence_md: str) -> Dict[str, bool]:
    return {
        "inputs": ("## Inputs" in evidence_md),
        "toolchain": ("## Toolchain" in evidence_md),
        "preflight": ("## Preflight checks" in evidence_md),
        "build": ("## Build" in evidence_md) or ("## Build (PyInstaller)" in evidence_md),
        "postcheck": ("## Post-build check" in evidence_md) or ("## Post-build" in evidence_md),
    }


def verify_release_root(root: Path) -> Tuple[Dict[str, Any], int]:
    required = ["app.exe", "config.example.json", "runbook.md", "evidence.md", "hashes.json"]

    details: Dict[str, Any] = {"root": str(root), "required_files": required}

    ok_files, missing = require_files(root, required)
    details["structure"] = {"ok": ok_files, "missing": missing}

    exe = root / "app.exe"
    smoke_help = run([str(exe), "--help"], cwd=root)
    smoke_ping = run([str(exe), "ping"], cwd=root)
    details["smoke"] = {"help": smoke_help, "ping": smoke_ping}

    # hashes
    hashes_path = root / "hashes.json"
    hashes_obj = json.loads(hashes_path.read_text(encoding="utf-8-sig"))
    declared = hashes_obj.get("files", {}) if isinstance(hashes_obj, dict) else {}
    computed: Dict[str, str] = {}
    for name in ["app.exe", "config.example.json", "runbook.md", "evidence.md"]:
        computed[name] = sha256_file(root / name)
    details["hashes"] = {"declared": declared, "computed": computed}

    hashes_ok = True
    for k, v in computed.items():
        dv = declared.get(k)
        if not dv or str(dv).lower() != v.lower():
            hashes_ok = False

    evidence_md = read_text_safe(root / "evidence.md")
    sec = evidence_has_sections(evidence_md)
    details["evidence"] = {"len": len(evidence_md), "sections": sec}

    all_ok = (
        ok_files
        and smoke_help["rc"] == 0
        and smoke_ping["rc"] == 0
        and hashes_ok
        and all(sec.values())
    )

    return details, (0 if all_ok else 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Acceptance gate v1 (zip or dir)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--zip", help="Path to release zip")
    g.add_argument("--dir", help="Path to release directory (e.g., dist/<product_id>)")
    ap.add_argument("--out", required=False, default=None, help="Optional JSON output path (also prints to stdout)")
    args = ap.parse_args()

    try:
        if args.zip:
            zip_path = Path(args.zip).resolve()
            if not zip_path.exists():
                return emit({"error": {"kind": "not_found", "message": f"zip not found: {zip_path}"}}, 1)

            tmp_root = Path(tempfile.mkdtemp(prefix="acceptance_gate_v1__"))
            extracted = tmp_root / "release"
            extracted.mkdir(parents=True, exist_ok=True)
            extract_zip(zip_path, extracted)

            checks, rc = verify_release_root(extracted)
            payload: Dict[str, Any] = {"mode": "zip", "zip": str(zip_path), "checks": checks, "tmp": str(tmp_root)}

        else:
            root = Path(args.dir).resolve()
            if not root.exists():
                return emit({"error": {"kind": "not_found", "message": f"dir not found: {root}"}}, 1)

            checks, rc = verify_release_root(root)
            payload = {"mode": "dir", "dir": str(root), "checks": checks}

        if args.out:
            out_path = Path(args.out).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps({**payload, "schema": SCHEMA, "ts_utc": utc_ts(), "ok": (rc == 0), "exit_code": rc}, ensure_ascii=False),
                encoding="utf-8",
            )

        return emit(payload, rc)

    except Exception as e:
        return emit({"error": {"kind": e.__class__.__name__, "message": str(e)}}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
