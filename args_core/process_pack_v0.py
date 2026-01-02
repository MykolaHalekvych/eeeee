from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .common_v0 import atomic_write_json, utc_now_iso


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_last_jsonl(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    last: Optional[Dict[str, Any]] = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    last = obj
            except Exception:
                continue
    return last


def forensic(run_dir: Path) -> Dict[str, Any]:
    state = _read_json(run_dir / "state.json")
    health = _read_json(run_dir / "health.json")
    rec_last = _read_last_jsonl(run_dir / "reconcile.jsonl")

    counters: Dict[str, Any] = {}
    last_error = None
    if isinstance(state, dict):
        counters = state.get("counters") or {}
        last_error = state.get("last_error")

    ratio = None
    if isinstance(rec_last, dict):
        ratio = rec_last.get("ratio")

    rep: Dict[str, Any] = {
        "schema": "forensic_report_v0",
        "ts_utc": utc_now_iso(),
        "run_dir": str(run_dir),
        "summary": {
            "reconcile_ratio_last": float(ratio) if ratio is not None else None,
            "events_seen": int((counters or {}).get("events_seen", 0) or 0),
            "forbidden": int((counters or {}).get("forbidden", 0) or 0),
            "dedup_skips": int((counters or {}).get("dedup_skips", 0) or 0),
            "last_error": last_error,
        },
        "paths": {
            "state": str(run_dir / "state.json"),
            "health": str(run_dir / "health.json"),
            "ledger": str(run_dir / "ledger.jsonl"),
            "events": str(run_dir / "events.jsonl"),
            "reconcile": str(run_dir / "reconcile.jsonl"),
        },
        "health": health if isinstance(health, dict) else None,
    }

    out = run_dir / "forensic_report.json"
    atomic_write_json(out, rep)
    rep["out_path"] = str(out)
    return rep


def _run(cmd: list[str], cwd: Path) -> Dict[str, Any]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}


def _run_git(repo: Path, *args: str) -> Dict[str, Any]:
    return _run(["git", *args], repo)


def _git_status_porcelain(repo: Path) -> Dict[str, Any]:
    """
    Returns dict:
      { ok: bool, porcelain: str, error: str|None, returncode: int }
    Fail-closed: if git fails, ok=False and porcelain contains marker.
    """
    p = _run_git(repo, "status", "--porcelain")
    if p["returncode"] != 0:
        err = (p["stderr"] or "").strip()
        return {
            "ok": False,
            "porcelain": "",
            "error": f"git_status_failed rc={p['returncode']} stderr={err}",
            "returncode": int(p["returncode"]),
        }
    return {"ok": True, "porcelain": (p["stdout"] or "").strip(), "error": None, "returncode": 0}


def _git_head(repo: Path) -> Dict[str, Any]:
    p = _run_git(repo, "rev-parse", "HEAD")
    if p["returncode"] != 0:
        err = (p["stderr"] or "").strip()
        return {"ok": False, "head": "", "error": f"git_head_failed rc={p['returncode']} stderr={err}"}
    return {"ok": True, "head": (p["stdout"] or "").strip(), "error": None}


@dataclass
class ReleaseGateError(Exception):
    payload: Dict[str, Any]
    exit_code: int = 2


def release(
    repo: Path,
    tag: str,
    notes: str,
    *,
    require_clean_tree: bool = True,
    allow_dirty: bool = False,
    create_git_tag: bool = False,
) -> Dict[str, Any]:
    repo = repo.resolve()
    rel_dir = repo / "releases" / tag
    rel_dir.mkdir(parents=True, exist_ok=True)

    # --- git info (fail-closed for governance) ---
    st = _git_status_porcelain(repo)
    head = _git_head(repo)

    is_dirty = False
    if st["ok"]:
        is_dirty = bool(st["porcelain"])
    else:
        # git error counts as dirty unless allow_dirty
        is_dirty = True

    if require_clean_tree and is_dirty and (not allow_dirty):
        payload = {
            "ok": False,
            "reason": "DIRTY_TREE",
            "ts_utc": utc_now_iso(),
            "tag": tag,
            "repo": str(repo),
            "require_clean_tree": True,
            "allow_dirty": False,
            "git_head": head.get("head", ""),
            "git_status_ok": st["ok"],
            "git_status_porcelain": st["porcelain"],
            "git_error": st["error"],
        }
        raise ReleaseGateError(payload=payload, exit_code=2)

    # --- tests evidence ---
    tests = _run(["py", "-3.11", "run_tests_core.py"], repo)
    (rel_dir / "tests.stdout.txt").write_text(tests["stdout"], encoding="utf-8")
    (rel_dir / "tests.stderr.txt").write_text(tests["stderr"], encoding="utf-8")

    # --- diff evidence ---
    diff = _run_git(repo, "diff")
    (rel_dir / "diff.patch").write_text(diff["stdout"], encoding="utf-8")

    # --- status evidence ---
    (rel_dir / "git_status_porcelain.txt").write_text(st["porcelain"] or "", encoding="utf-8")
    if st["error"]:
        (rel_dir / "git_status_error.txt").write_text(st["error"], encoding="utf-8")

    head_sha = head.get("head", "")

    # --- optional git tag ---
    tag_created = False
    tag_error: Optional[str] = None
    if create_git_tag:
        # lightweight tag is enough; do not fail release if tag already exists
        ptag = _run_git(repo, "tag", tag)
        tag_created = (ptag["returncode"] == 0)
        if not tag_created:
            tag_error = (ptag["stderr"] or "").strip()

    manifest: Dict[str, Any] = {
        "schema": "release_manifest_v1",
        "ts_utc": utc_now_iso(),
        "tag": tag,
        "notes": notes,
        "repo": str(repo),

        "git": {
            "head": head_sha,
            "status_ok": bool(st["ok"]),
            "status_porcelain": st["porcelain"],
            "status_error": st["error"],
            "require_clean_tree": bool(require_clean_tree),
            "allow_dirty": bool(allow_dirty),
            "tag_requested": bool(create_git_tag),
            "tag_created": bool(tag_created),
            "tag_error": tag_error,
        },

        "tests": {
            "returncode": int(tests["returncode"]),
            "cmd": tests["cmd"],
            "paths": {
                "stdout": str(rel_dir / "tests.stdout.txt"),
                "stderr": str(rel_dir / "tests.stderr.txt"),
            },
        },

        "paths": {
            "diff": str(rel_dir / "diff.patch"),
            "git_status_porcelain": str(rel_dir / "git_status_porcelain.txt"),
            "manifest": str(rel_dir / "manifest.json"),
        },
    }

    atomic_write_json(rel_dir / "manifest.json", manifest)

    # changelog append (simple)
    ch = repo / "CHANGELOG.md"
    line = f"- {utc_now_iso()}  {tag}  tests={tests['returncode']}  head={head_sha}  clean_required={require_clean_tree}  allow_dirty={allow_dirty}\n"
    prev = ch.read_text(encoding="utf-8") if ch.exists() else "# CHANGELOG\n\n"
    ch.write_text(prev + line, encoding="utf-8")

    manifest["out_dir"] = str(rel_dir)
    manifest["ok"] = True
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_f = sub.add_parser("forensic")
    ap_f.add_argument("--run-dir", required=True)

    ap_r = sub.add_parser("release")
    ap_r.add_argument("--repo", required=True)
    ap_r.add_argument("--tag", required=True)
    ap_r.add_argument("--notes", default="")

    # governance gate
    ap_r.add_argument(
        "--require-clean-tree",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Require clean git working tree (default true). Use --no-require-clean-tree to disable.",
    )
    ap_r.add_argument("--allow-dirty", action="store_true", default=False, help="Override clean-tree gate.")
    ap_r.add_argument("--create-git-tag", action="store_true", default=False)

    args = ap.parse_args()

    if args.cmd == "forensic":
        rep = forensic(Path(args.run_dir))
        print(json.dumps(rep, ensure_ascii=False))
        return 0

    if args.cmd == "release":
        try:
            rep = release(
                Path(args.repo),
                str(args.tag),
                str(args.notes),
                require_clean_tree=bool(args.require_clean_tree),
                allow_dirty=bool(args.allow_dirty),
                create_git_tag=bool(args.create_git_tag),
            )
            print(json.dumps(rep, ensure_ascii=False))
            return 0
        except ReleaseGateError as e:
            print(json.dumps(e.payload, ensure_ascii=False))
            return int(e.exit_code)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
