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