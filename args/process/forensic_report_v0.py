from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from args.offline.evidence_history_v0 import (
    atomic_write_json,
    find_repo_root,
    load_json_tolerant,
    sha256_file,
    utc_compact,
    utc_iso,
    utc_now,
)

ALLOWED_EXTS = {".json", ".jsonl", ".log", ".txt", ".md"}


def _git_info(root: Path) -> Dict[str, Any]:
    if not (root / ".git").exists():
        return {"is_git_repo": False}

    def _run(cmd: Sequence[str]) -> Tuple[int, str]:
        try:
            p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, shell=False)
            return p.returncode, p.stdout.strip()
        except Exception:
            return 1, ""

    rc, head = _run(["git", "rev-parse", "HEAD"])
    rc2, por = _run(["git", "status", "--porcelain"])
    return {"is_git_repo": True, "head": head if rc == 0 else None, "dirty": bool(por) if rc2 == 0 else None}


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path)


def _load_latest_run_id(root: Path) -> Optional[str]:
    p = root / "args" / "data" / "latest_run_id.txt"
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace").strip() or None


def _collect_files(root: Path, run_id: str, *, include_archives: bool, max_files: int) -> List[Path]:
    base = [
        root / "args" / "data" / "control_state.json",
        root / "args" / "data" / "latest_run_id.txt",
        root / "args" / "data" / "latest_paths.json",
        root / "args" / "data" / "ops_health.json",
        root / "args" / "data" / "auto_loop.lock.json",
        root / "args" / "logs" / "ops_events.jsonl",
        root / "args" / "offline" / "model_registry.json",
        root / "args" / "offline" / "model_registry_events.jsonl",
        root / "args" / "data" / "as_model_version.json",
        root / "args" / "offline" / "evidence" / "eval_gate" / "latest.json",
    ]
    out = [p for p in base if p.exists()]
    seen = {p.resolve() for p in out}

    search_dirs = [root / "args" / "logs", root / "args" / "data", root / "args" / "offline"]
    if include_archives:
        search_dirs.append(root / "args" / "logs" / "archive")

    for d in search_dirs:
        if not d.exists():
            continue
        for p in d.rglob(f"*{run_id}*"):
            if len(out) >= max_files:
                break
            if p.is_dir() or p.suffix.lower() not in ALLOWED_EXTS:
                continue
            rp = p.resolve()
            if rp in seen:
                continue
            seen.add(rp)
            out.append(p)

    return out


def _ops_events_excerpt(run_id: str, paths: Sequence[Path], *, tail: int) -> List[Dict[str, Any]]:
    q: Deque[Dict[str, Any]] = deque(maxlen=tail)
    for p in paths:
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                    except Exception:
                        continue
                    if isinstance(obj, dict) and str(obj.get("run_id", "")) == str(run_id):
                        q.append(obj)
        except Exception:
            continue
    return list(q)


def _file_inventory(root: Path, paths: Sequence[Path]) -> List[Dict[str, Any]]:
    inv = []
    for p in paths:
        try:
            st = p.stat()
        except Exception:
            continue
        inv.append(
            {
                "path": _safe_rel(p, root),
                "bytes": st.st_size,
                "mtime_utc": utc_iso(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)),
                "sha256": sha256_file(p),
            }
        )
    inv.sort(key=lambda x: x["path"])
    return inv


def _zip_bundle(root: Path, out_zip: Path, include_paths: Sequence[Path]) -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in include_paths:
            if p.exists() and p.is_file():
                z.write(p, arcname=_safe_rel(p, root))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ARGS Process Pack: forensic_report (v0)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--run_id")
    g.add_argument("--latest", action="store_true")
    ap.add_argument("--out_dir", default="args/process/forensics")
    ap.add_argument("--include_archives", action="store_true")
    ap.add_argument("--max_files", type=int, default=200)
    ap.add_argument("--ops_events_tail", type=int, default=200)
    ap.add_argument("--zip", action="store_true")
    ns = ap.parse_args(argv)

    root = find_repo_root()
    run_id = ns.run_id or (_load_latest_run_id(root) if ns.latest else None)
    if not run_id:
        print("FAIL: run_id not resolved (missing args/data/latest_run_id.txt?)", file=sys.stderr)
        return 2

    now = utc_now()
    ts = utc_compact(now)

    out_dir = (root / ns.out_dir) if not Path(ns.out_dir).is_absolute() else Path(ns.out_dir)
    run_dir = out_dir / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    files = _collect_files(root, str(run_id), include_archives=bool(ns.include_archives), max_files=int(ns.max_files))

    ops_paths = []
    cur = root / "args" / "logs" / "ops_events.jsonl"
    if cur.exists():
        ops_paths.append(cur)
    if ns.include_archives:
        arch = root / "args" / "logs" / "archive" / "ops_events"
        if arch.exists():
            ops_paths.extend([p for p in arch.rglob("*.jsonl") if p.is_file()])
    excerpt = _ops_events_excerpt(str(run_id), ops_paths, tail=int(ns.ops_events_tail))

    eval_latest = root / "args" / "offline" / "evidence" / "eval_gate" / "latest.json"
    eval_block: Dict[str, Any] = {"found": False}
    if eval_latest.exists():
        try:
            ptr = load_json_tolerant(eval_latest)
            eval_block = {"found": True, "latest_pointer_path": _safe_rel(eval_latest, root), "latest_pointer": ptr}
            rp = ptr.get("latest_report_path") if isinstance(ptr, dict) else None
            if isinstance(rp, str) and rp.strip():
                report_abs = (root / rp) if not Path(rp).is_absolute() else Path(rp)
                if report_abs.exists():
                    eval_block["latest_report_path"] = _safe_rel(report_abs, root)
                    try:
                        eval_block["latest_report_obj"] = load_json_tolerant(report_abs)
                    except Exception:
                        pass
        except Exception as e:
            eval_block = {"found": False, "error": repr(e)}

    report = {
        "kind": "forensic_report_v0",
        "generated_at_utc": utc_iso(now),
        "run_id": str(run_id),
        "host": {"platform": platform.platform(), "python": sys.version, "pid": os.getpid()},
        "repo": {"root": str(root), "git": _git_info(root)},
        "inputs": {"include_archives": bool(ns.include_archives), "max_files": int(ns.max_files), "ops_events_tail": int(ns.ops_events_tail)},
        "eval_evidence": eval_block,
        "ops_events_excerpt": excerpt,
        "files": _file_inventory(root, files),
    }

    report_path = run_dir / f"forensic_report_{ts}.json"
    atomic_write_json(report_path, report)

    latest_ptr = {
        "kind": "forensic_report_latest_pointer_v1",
        "updated_at_utc": utc_iso(now),
        "run_id": str(run_id),
        "latest_report_path": _safe_rel(report_path, root),
        "latest_report_sha256": sha256_file(report_path),
    }
    atomic_write_json(run_dir / "latest.json", latest_ptr)
    atomic_write_json(out_dir / "latest.json", latest_ptr)

    if ns.zip:
        zip_path = run_dir / f"forensic_bundle_{ts}.zip"
        include = [report_path]
        for p in [
            root / "args" / "data" / "control_state.json",
            root / "args" / "data" / "latest_paths.json",
            root / "args" / "data" / "ops_health.json",
            root / "args" / "data" / "as_model_version.json",
            root / "args" / "offline" / "model_registry.json",
            root / "args" / "offline" / "model_registry_events.jsonl",
            root / "args" / "offline" / "evidence" / "eval_gate" / "latest.json",
        ]:
            if p.exists():
                include.append(p)
        _zip_bundle(root, zip_path, include)

    print(f"PASS: wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())