from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2

BANNED_TARGETS = {"app.exe", "hashes.json", "acceptance_gate.json", "release_manifest_v1.json"}


def utc_now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(obj: Dict[str, Any], code: int) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    sys.exit(int(code))


def classify_exception(exc: Exception) -> Tuple[int, str]:
    if isinstance(exc, (FileNotFoundError, ValueError, json.JSONDecodeError)):
        return RC_FAIL, "fail"
    if isinstance(exc, (PermissionError, OSError)):
        return RC_INFRA, "infra"
    return RC_INFRA, "infra"


def read_json_utf8sig(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    if tmp.exists():
        tmp.unlink(missing_ok=True)
    with tmp.open("wb") as f:
        f.write(data)
    if path.exists():
        path.unlink(missing_ok=True)
    tmp.replace(path)


def norm_rel(p: str) -> str:
    p = (p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def ensure_safe_basename(p: str) -> str:
    p = norm_rel(p)
    if not p:
        raise ValueError("empty_path")
    if p.startswith("/") or p.startswith("\\"):
        raise ValueError("abs_path")
    if ":" in p:
        raise ValueError("drive_path")
    base = Path(p).name
    if base != p:
        raise ValueError("not_flat_path:" + p)
    if base in ("", ".", ".."):
        raise ValueError("bad_basename")
    if base in BANNED_TARGETS:
        raise ValueError("banned_target:" + base)
    return base


def load_request_allowlist(req_path: Path) -> Tuple[str, List[str], str]:
    j = read_json_utf8sig(req_path)
    if not isinstance(j, dict):
        raise ValueError("request_not_object")
    if str(j.get("schema") or "") != "customization_request_v1":
        raise ValueError("schema_invalid")

    overlay_dir = str(j.get("overlay_dir") or "").strip()
    if not overlay_dir:
        raise ValueError("overlay_dir_missing")
    overlay_dir = norm_rel(overlay_dir)

    allow = j.get("allowlist")
    if not isinstance(allow, list) or len(allow) == 0:
        raise ValueError("allowlist_missing_or_empty")

    out: List[str] = []
    for x in allow:
        if not isinstance(x, str):
            raise ValueError("allowlist_item_not_string")
        out.append(ensure_safe_basename(x))

    # request_id only for reporting
    req_id = str(j.get("request_id") or "").strip()
    return overlay_dir, out, req_id


def main_inner() -> Tuple[Dict[str, Any], int]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--request", required=True)
    ap.add_argument("--workspace-dir", required=True)
    ap.add_argument("--out-report", default="")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    req_path = Path(args.request).resolve()
    ws_dir = Path(args.workspace_dir).resolve()
    out_report = Path(args.out_report).resolve() if args.out_report.strip() else None

    if not ws_dir.exists() or not ws_dir.is_dir():
        raise FileNotFoundError(f"workspace_dir_missing:{ws_dir}")

    overlay_rel, allowlist, req_id = load_request_allowlist(req_path)

    # overlay_dir must be inside repo in v1 (reproducible & safe)
    overlay_dir = (repo / overlay_rel).resolve()
    if not overlay_dir.exists() or not overlay_dir.is_dir():
        raise FileNotFoundError(f"overlay_dir_missing:{overlay_dir}")
    if not overlay_dir.is_relative_to(repo):
        raise ValueError("overlay_dir_outside_repo")

    allow_set_lower: Set[str] = {a.lower() for a in allowlist}

    # overlay must be flat and contain NO extra files beyond allowlist
    overlay_files = [p for p in overlay_dir.iterdir() if p.is_file()]
    overlay_names = [p.name for p in overlay_files]
    extra = [n for n in overlay_names if n.lower() not in allow_set_lower]
    if extra:
        raise ValueError("overlay_extra_files:" + ",".join(sorted(extra)))

    applied: List[Dict[str, Any]] = []
    skipped_missing: List[str] = []

    for name in allowlist:
        src = overlay_dir / name
        if not src.exists():
            skipped_missing.append(name)
            continue
        dst = ws_dir / name
        data = src.read_bytes()
        _atomic_write(dst, data)
        applied.append({"name": name, "bytes": len(data), "sha256": sha256_file(dst)})

    rep = {
        "schema": "overlay_apply_report_v1",
        "ts_utc": utc_now_iso(),
        "request_id": req_id,
        "request_path": str(req_path),
        "overlay_dir": str(overlay_dir),
        "workspace_dir": str(ws_dir),
        "allowlist": allowlist,
        "applied": applied,
        "skipped_missing": skipped_missing,
        "count_applied": len(applied),
        "count_skipped_missing": len(skipped_missing),
    }

    if out_report is not None:
        out_report.parent.mkdir(parents=True, exist_ok=True)
        out_report.write_text(json.dumps(rep, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    payload = {
        "schema": "overlay_apply_v1",
        "ts_utc": utc_now_iso(),
        "ok": True,
        "exit_code": 0,
        "request_path": str(req_path),
        "workspace_dir": str(ws_dir),
        "overlay_dir": str(overlay_dir),
        "count_applied": len(applied),
        "count_skipped_missing": len(skipped_missing),
        "out_report": str(out_report) if out_report is not None else "",
    }
    return payload, RC_OK


def main() -> None:
    try:
        payload, code = main_inner()
        emit(payload, code)
    except Exception as e:
        code, kind = classify_exception(e)
        emit(
            {
                "schema": "overlay_apply_v1",
                "ts_utc": utc_now_iso(),
                "ok": False,
                "exit_code": code,
                "error": {"kind": kind, "type": type(e).__name__, "message": str(e)},
            },
            code,
        )


if __name__ == "__main__":
    main()