# scripts/stage9_process_files.ps1
# Creates Stage 9 Process Pack files without git patch.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

function Write-Utf8NoBom([string]$RelPath, [string]$Content) {
  $Abs = Join-Path $Root $RelPath
  $Dir = Split-Path -Parent $Abs
  if (!(Test-Path $Dir)) { New-Item -ItemType Directory -Force -Path $Dir | Out-Null }
  $enc = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Abs, $Content.Replace("`r`n","`n"), $enc)
  Write-Host "WROTE $RelPath"
}

# --- args/process/forensic_report_v0.py
Write-Utf8NoBom "args/process/forensic_report_v0.py" @'
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
'@

# --- args/process/release_tag_v0.py
Write-Utf8NoBom "args/process/release_tag_v0.py" @'
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from args.offline.evidence_history_v0 import (
    atomic_write_json,
    find_repo_root,
    load_json_tolerant,
    sha256_file,
    utc_compact,
    utc_iso,
    utc_now,
)


def _run_git(root: Path, cmd: Sequence[str]) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, shell=False)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", repr(e)


def _git_clean(root: Path) -> Tuple[bool, str]:
    rc, out, err = _run_git(root, ["git", "status", "--porcelain"])
    if rc != 0:
        return False, (err or out)
    return (out == ""), out


def _infer_eval_pass(report: Dict[str, Any]) -> Optional[bool]:
    st = report.get("status")
    if isinstance(st, str):
        if st.upper() == "PASS":
            return True
        if st.upper() == "FAIL":
            return False
    for k in ("pass", "passed", "ok", "success"):
        v = report.get(k)
        if isinstance(v, bool):
            return v
    return None


def _infer_model_id(report: Dict[str, Any]) -> Optional[str]:
    v = report.get("model_id")
    if isinstance(v, str) and v.strip():
        return v.strip()
    m = report.get("model")
    if isinstance(m, dict):
        mid = m.get("id") or m.get("model_id")
        if isinstance(mid, str) and mid.strip():
            return mid.strip()
    return None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ARGS Process Pack: release tagging (v0)")
    ap.add_argument("--tag", help="Override tag name (default is generated)")
    ap.add_argument("--allow_dirty", action="store_true")
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--message", help="Extra note line for the tag annotation")
    ns = ap.parse_args(argv)

    root = find_repo_root()
    if not (root / ".git").exists():
        print("FAIL: .git not found (not a git repo?)", file=sys.stderr)
        return 3

    if not ns.allow_dirty:
        ok, _ = _git_clean(root)
        if not ok:
            print("FAIL: git working tree is dirty; commit/stash or use --allow_dirty", file=sys.stderr)
            return 3

    as_mv_path = root / "args" / "data" / "as_model_version.json"
    if not as_mv_path.exists():
        print(f"FAIL: missing {as_mv_path}", file=sys.stderr)
        return 2
    as_mv = load_json_tolerant(as_mv_path)
    promoted = str(as_mv.get("as_model_version")) if isinstance(as_mv, dict) else ""
    if not promoted:
        print(f"FAIL: invalid as_model_version.json: {as_mv_path}", file=sys.stderr)
        return 2

    latest_ptr_path = root / "args" / "offline" / "evidence" / "eval_gate" / "latest.json"
    if not latest_ptr_path.exists():
        print(f"FAIL: missing {latest_ptr_path}", file=sys.stderr)
        return 2
    ptr = load_json_tolerant(latest_ptr_path)
    if not isinstance(ptr, dict) or not isinstance(ptr.get("latest_report_path"), str):
        print(f"FAIL: invalid latest pointer: {latest_ptr_path}", file=sys.stderr)
        return 2

    report_abs = root / ptr["latest_report_path"]
    if not report_abs.exists():
        print(f"FAIL: eval report missing: {report_abs}", file=sys.stderr)
        return 2

    report = load_json_tolerant(report_abs)
    if not isinstance(report, dict):
        print(f"FAIL: eval report not an object: {report_abs}", file=sys.stderr)
        return 2

    if _infer_eval_pass(report) is not True:
        print("FAIL: latest eval evidence is not PASS", file=sys.stderr)
        return 2

    eval_mid = _infer_model_id(report)
    if eval_mid and eval_mid != promoted:
        print(f"FAIL: model mismatch promoted={promoted} eval={eval_mid}", file=sys.stderr)
        return 2

    now = utc_now()
    ts = utc_compact(now)
    tag = ns.tag or f"args-core-v1-model-{promoted}-{ts}"

    msg_lines = [
        "ARGS-Core-v1 release tag",
        f"created_at_utc: {utc_iso(now)}",
        f"promoted_model_id: {promoted}",
        f"eval_report_path: {report_abs.as_posix()}",
        f"eval_report_sha256: {sha256_file(report_abs)}",
    ]
    if ns.message:
        msg_lines.append(f"note: {ns.message}")
    msg = "\n".join(msg_lines)

    if ns.dry_run:
        print("DRY RUN")
        print(f"tag: {tag}")
        print(msg)
        return 0

    rc, out, err = _run_git(root, ["git", "tag", "-a", tag, "-m", msg])
    if rc != 0:
        print(f"FAIL: git tag failed: {err or out}", file=sys.stderr)
        return 3

    rel_dir = root / "args" / "process" / "releases"
    rel_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "kind": "release_tag_artifact_v0",
        "created_at_utc": utc_iso(now),
        "tag": tag,
        "promoted_model_id": promoted,
        "eval_report_path": str(report_abs),
        "eval_report_sha256": sha256_file(report_abs),
    }
    out_path = rel_dir / f"release_tag_{ts}.json"
    atomic_write_json(out_path, artifact)
    atomic_write_json(rel_dir / "latest.json", artifact)

    print(f"PASS: created git tag {tag}")
    print(f"wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'@

Write-Host "DONE."
