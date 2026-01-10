from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.foundry.retention_policy_v1 import load_retention_policy_v1


RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{uuid.uuid4().hex[:8]}"


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True), encoding="utf-8", newline="\n")


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def resolve_path(p: Path) -> Path:
    return p.resolve()


def ensure_under(root: Path, p: Path) -> bool:
    rp = resolve_path(p)
    rr = resolve_path(root)
    try:
        return rp.is_relative_to(rr)  # py3.11
    except Exception:
        return False


def parse_release_id(fname: str) -> Tuple[str, str] | None:
    base = fname
    if base.endswith(".hashes.json"):
        base = base[: -len(".hashes.json")]
    elif base.endswith(".zip"):
        base = base[: -len(".zip")]
    else:
        return None
    if "__" not in base:
        return None
    product_id = base.split("__", 1)[0]
    return product_id, base


def plan_dist_releases(releases_dir: Path, keep_last_per_product: int, pinned_release_ids: List[str]) -> Dict[str, Any]:
    files = [p for p in releases_dir.glob("*") if p.is_file()]
    by_product: Dict[str, List[Tuple[float, str, Path]]] = {}
    for p in files:
        pr = parse_release_id(p.name)
        if not pr:
            continue
        product_id, release_id = pr
        by_product.setdefault(product_id, []).append((p.stat().st_mtime, release_id, p))

    keep_ids = set(pinned_release_ids)
    for product_id, rows in by_product.items():
        rows_sorted = sorted(rows, key=lambda x: x[0], reverse=True)
        top_ids: List[str] = []
        for _, rid, _ in rows_sorted:
            if rid not in top_ids:
                top_ids.append(rid)
            if len(top_ids) >= keep_last_per_product:
                break
        keep_ids.update(top_ids)

    delete_files: List[str] = []
    for _, rows in by_product.items():
        for _, rid, p in rows:
            if rid not in keep_ids:
                delete_files.append(str(p))

    return {
        "releases_dir": str(releases_dir),
        "keep_ids": sorted(list(keep_ids)),
        "delete_files": sorted(delete_files),
    }


def check_pinned_exist(releases_dir: Path, pinned_release_ids: List[str]) -> List[str]:
    missing: List[str] = []
    for rid in pinned_release_ids:
        z = releases_dir / f"{rid}.zip"
        h = releases_dir / f"{rid}.hashes.json"
        if not z.exists():
            missing.append(str(z))
        if not h.exists():
            missing.append(str(h))
    return missing


def classify_run(final_report_path: Path) -> str:
    try:
        j = json.loads(final_report_path.read_text(encoding="utf-8"))
        ok = bool(j.get("ok", False))
        ec = int(j.get("exit_code", 2))
        if ok and ec == 0:
            return "PASS"
        if ec == 2:
            return "INFRA"
        return "FAIL"
    except Exception:
        return "UNKNOWN"


def plan_runs(runs_dir: Path, keep_last_pass: int, keep_days_fail_infra: int, keep_run_ids: List[str]) -> Dict[str, Any]:
    keep_set = set(keep_run_ids)

    dirs = [d for d in runs_dir.iterdir() if d.is_dir()]
    dirs_sorted = sorted(dirs, key=lambda p: p.name, reverse=True)

    pass_runs: List[Path] = []
    delete_dirs: List[str] = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days_fail_infra)

    for d in dirs_sorted:
        if d.name in keep_set:
            continue
        fr = d / "final_report.json"
        cls = "UNKNOWN"
        if fr.exists():
            cls = classify_run(fr)

        mtime = datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc)

        if cls == "PASS":
            pass_runs.append(d)
        elif cls in ("FAIL", "INFRA", "UNKNOWN"):
            if mtime < cutoff:
                delete_dirs.append(str(d))

    for d in pass_runs[keep_last_pass:]:
        delete_dirs.append(str(d))

    return {
        "runs_dir": str(runs_dir),
        "keep_run_ids": sorted(list(keep_set)),
        "delete_dirs": sorted(delete_dirs),
        "pass_total": len(pass_runs),
        "pass_keep": min(len(pass_runs), keep_last_pass),
    }


def plan_tmp(tmp_dir: Path, max_age_days: int) -> Dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    delete_files: List[str] = []
    delete_dirs: List[str] = []

    if not tmp_dir.exists():
        return {"tmp_dir": str(tmp_dir), "delete_files": [], "delete_dirs": []}

    for p in tmp_dir.rglob("*"):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        except Exception:
            continue
        if mtime < cutoff and p.is_file():
            delete_files.append(str(p))

    for d in sorted([x for x in tmp_dir.rglob("*") if x.is_dir()], key=lambda x: len(str(x)), reverse=True):
        try:
            if not any(d.iterdir()):
                mtime = datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    delete_dirs.append(str(d))
        except Exception:
            continue

    return {
        "tmp_dir": str(tmp_dir),
        "delete_files": sorted(delete_files),
        "delete_dirs": sorted(delete_dirs),
    }


def delete_path(p: Path) -> None:
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--policy", default="manifests/retention/retention_policy_v1.json")
    ap.add_argument("--mode", choices=["dryrun", "apply"], default="dryrun")
    ap.add_argument("--confirm-delete", default="NO")
    args = ap.parse_args()

    repo = Path(args.repo)
    run_id = utc_run_id()
    runs_dir_repo = repo / "args" / "data" / "runs"
    run_dir = runs_dir_repo / run_id
    evidence_dir = run_dir / "evidence"
    events_jsonl = run_dir / "events.jsonl"
    final_report_json = run_dir / "final_report.json"

    started = utc_now_iso()

    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        out = {
            "schema": "retention_final_report_v1",
            "ok": False,
            "exit_code": RC_INFRA,
            "mode": args.mode,
            "repo": str(repo),
            "run_id": run_id,
            "error": {"kind": "exception", "message": str(e)},
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return RC_INFRA

    def fail(code: int, kind: str, msg: str) -> int:
        ended = utc_now_iso()
        out = {
            "schema": "retention_final_report_v1",
            "ok": False,
            "exit_code": code,
            "mode": args.mode,
            "repo": str(repo),
            "policy_path": str(Path(args.policy)),
            "run_id": run_id,
            "run_dir": str(run_dir),
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_report_json),
            "started_utc": started,
            "ended_utc": ended,
            "error": {"kind": kind, "message": msg},
        }
        write_json(final_report_json, out)
        append_jsonl(events_jsonl, {"ts_utc": utc_now_iso(), "kind": "RETENTION_FAIL", "error": out["error"]})
        sys.stdout.write(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return code

    try:
        append_jsonl(events_jsonl, {"ts_utc": utc_now_iso(), "kind": "RETENTION_START", "mode": args.mode})
        policy = load_retention_policy_v1(args.policy)

        artifact_root = Path(policy.artifact_root)
        releases_dir = repo / Path(policy.releases_dir)
        runs_dir_cfg = repo / Path(policy.runs_dir)
        tmp_dir = repo / Path(policy.tmp_dir)

        if args.mode == "apply" and str(args.confirm_delete).upper() != "YES":
            return fail(RC_FAIL, "confirm_required", "apply_requires_confirm_delete_YES")

        if not releases_dir.exists():
            return fail(RC_INFRA, "missing_dir", f"releases_dir_not_found:{releases_dir}")

        missing_pins = check_pinned_exist(releases_dir, policy.pinned_release_ids)
        if missing_pins:
            write_json(evidence_dir / "retention_missing_pins.json", {"missing": missing_pins})
            return fail(RC_FAIL, "missing_pins", f"missing_pins:{len(missing_pins)}")

        dist_plan = plan_dist_releases(releases_dir, policy.keep_last_per_product, policy.pinned_release_ids)
        runs_plan = plan_runs(runs_dir_cfg, policy.keep_last_pass, policy.keep_days_fail_infra, policy.keep_run_ids)
        tmp_plan = plan_tmp(tmp_dir, policy.max_age_days)

        plan = {
            "schema": "retention_plan_v1",
            "ts_utc": utc_now_iso(),
            "repo": str(repo),
            "artifact_root": str(artifact_root),
            "dist": dist_plan,
            "runs": runs_plan,
            "tmp": tmp_plan,
        }

        if policy.require_paths_under_artifact_root:
            bad: List[str] = []
            candidates: List[Path] = []
            candidates += [Path(x) for x in dist_plan["delete_files"]]
            candidates += [Path(x) for x in runs_plan["delete_dirs"]]
            candidates += [Path(x) for x in tmp_plan["delete_files"]]
            candidates += [Path(x) for x in tmp_plan["delete_dirs"]]
            for c in candidates:
                if not ensure_under(artifact_root, c):
                    bad.append(str(c))
            if bad:
                write_json(evidence_dir / "retention_bad_paths.json", {"bad_paths": bad})
                return fail(RC_FAIL, "scope_violation", f"delete_targets_outside_artifact_root:{len(bad)}")

        plan_path = evidence_dir / "retention_plan_v1.json"
        write_json(plan_path, plan)
        append_jsonl(events_jsonl, {"ts_utc": utc_now_iso(), "kind": "RETENTION_PLAN_WRITTEN", "plan_path": str(plan_path)})

        applied: Dict[str, Any] | None = None
        if args.mode == "apply":
            applied = {"deleted_files": 0, "deleted_dirs": 0, "errors": []}
            for s in dist_plan["delete_files"] + tmp_plan["delete_files"]:
                try:
                    delete_path(Path(s))
                    applied["deleted_files"] += 1
                except Exception as e:
                    applied["errors"].append({"path": s, "error": str(e)})

            for s in runs_plan["delete_dirs"] + tmp_plan["delete_dirs"]:
                try:
                    delete_path(Path(s))
                    applied["deleted_dirs"] += 1
                except Exception as e:
                    applied["errors"].append({"path": s, "error": str(e)})

            write_json(evidence_dir / "retention_apply_v1.json", applied)
            append_jsonl(events_jsonl, {"ts_utc": utc_now_iso(), "kind": "RETENTION_APPLY_DONE", "summary": applied})

            if applied["errors"]:
                return fail(RC_INFRA, "delete_errors", f"delete_errors:{len(applied['errors'])}")

        ended = utc_now_iso()
        out = {
            "schema": "retention_final_report_v1",
            "ok": True,
            "exit_code": RC_OK,
            "mode": args.mode,
            "repo": str(repo),
            "artifact_root": str(artifact_root),
            "policy_path": str(Path(args.policy)),
            "run_id": run_id,
            "run_dir": str(run_dir),
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_report_json),
            "started_utc": started,
            "ended_utc": ended,
            "plan_path": str(plan_path),
            "plan_summary": {
                "dist_delete_files": len(dist_plan["delete_files"]),
                "runs_delete_dirs": len(runs_plan["delete_dirs"]),
                "tmp_delete_files": len(tmp_plan["delete_files"]),
                "tmp_delete_dirs": len(tmp_plan["delete_dirs"]),
            },
            "apply_summary": applied,
        }
        write_json(final_report_json, out)
        append_jsonl(events_jsonl, {"ts_utc": utc_now_iso(), "kind": "RETENTION_OK"})
        sys.stdout.write(json.dumps(out, ensure_ascii=False, sort_keys=True))
        return RC_OK

    except ValueError as ve:
        return fail(RC_FAIL, "policy_error", str(ve))
    except Exception as e:
        return fail(RC_INFRA, "exception", str(e))


if __name__ == "__main__":
    raise SystemExit(main())