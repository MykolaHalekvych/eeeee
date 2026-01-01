#!/usr/bin/env python3
"""
args.ops.rotate_logs_v0

Stage7.5 housekeeping:

A) Latest pointers
   - args/data/latest_run_id.txt
   - args/data/latest_paths.json

B) Rotation / archiving
   - args/logs/ops_<cycle>_<step>.log  (keeps last N)
   - args/logs/run_report_<runid>_*.json (keeps last N)
   - args/data/*_<runid>.(json|jsonl) for known per-run prefixes (keeps last M run_ids)

Notes:
- This is intentionally deterministic: count-based retention, no cloud deps.
- Rotation is best-effort. Any move failures are reported and result in non-zero exit code.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

_SCHEMA = "rotate_logs_v0"


def _utc_now_z() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(_read_text(path))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class RotateCfg:
    enabled: bool = True
    keep_ops_logs: int = 500
    keep_run_reports: int = 500
    keep_run_artifacts: int = 300
    archive_dir: Optional[str] = None  # if relative -> args/logs/<archive_dir>
    # If set, only archive files strictly older than this many days (in addition to count-based rules).
    max_age_days: Optional[int] = None


def _load_rotate_cfg(control_state_path: Path) -> RotateCfg:
    cfg = RotateCfg()
    if not control_state_path.exists():
        return cfg

    try:
        d = _read_json(control_state_path)
    except Exception:
        return cfg

    ops_loop = d.get("ops_loop") or {}
    rot = ops_loop.get("rotate") or {}
    if not isinstance(rot, dict):
        return cfg

    def _get_int(key: str, default: int) -> int:
        v = rot.get(key, default)
        try:
            return int(v)
        except Exception:
            return default

    enabled = bool(rot.get("enabled", cfg.enabled))
    keep_ops_logs = _get_int("keep_ops_logs", cfg.keep_ops_logs)
    keep_run_reports = _get_int("keep_run_reports", cfg.keep_run_reports)

    # Accept both names, prefer explicit keep_run_artifacts if present
    keep_run_artifacts = cfg.keep_run_artifacts
    if "keep_run_ids" in rot:
        keep_run_artifacts = _get_int("keep_run_ids", cfg.keep_run_artifacts)
    if "keep_run_artifacts" in rot:
        keep_run_artifacts = _get_int("keep_run_artifacts", keep_run_artifacts)

    archive_dir = rot.get("archive_dir", cfg.archive_dir)
    if archive_dir is not None:
        archive_dir = str(archive_dir)

    max_age_days = rot.get("max_age_days", cfg.max_age_days)
    if max_age_days is not None:
        try:
            max_age_days = int(max_age_days)
        except Exception:
            max_age_days = cfg.max_age_days

    return RotateCfg(
        enabled=enabled,
        keep_ops_logs=max(0, keep_ops_logs),
        keep_run_reports=max(0, keep_run_reports),
        keep_run_artifacts=max(0, keep_run_artifacts),
        archive_dir=archive_dir,
        max_age_days=max_age_days,
    )


_RE_OPS_STEP_LOG = re.compile(r"^ops_\d{8}T\d{6}Z_\d{2}_.+\.log$", re.IGNORECASE)
_RE_RUN_REPORT = re.compile(r"^run_report_([0-9a-f]{6,64})_.+\.json$", re.IGNORECASE)

# Per-run data artifacts we are allowed to archive (conservative allowlist)
_RUN_DATA_PREFIXES = (
    "events_run_",
    "orders_paper_",
    "orders_sendplan_",
    "orders_exec_",
    "sent_orders_",
    "would_send_",
)

_RE_RUNID_ANYWHERE = re.compile(r"([0-9a-f]{6,64})", re.IGNORECASE)


def _is_ops_step_log(path: Path) -> bool:
    return bool(_RE_OPS_STEP_LOG.match(path.name))


def _is_run_report(path: Path) -> bool:
    return bool(_RE_RUN_REPORT.match(path.name))


def _extract_run_id_from_run_report(path: Path) -> Optional[str]:
    m = _RE_RUN_REPORT.match(path.name)
    if not m:
        return None
    return m.group(1).lower()


def _extract_run_id_from_data_artifact(path: Path) -> Optional[str]:
    name = path.name
    if not any(name.startswith(p) for p in _RUN_DATA_PREFIXES):
        return None
    # expected: <prefix><runid>.<ext>
    m = re.match(r"^[a-z_]+_([0-9a-f]{6,64})\.(jsonl|json)$", name, flags=re.IGNORECASE)
    if m:
        return m.group(1).lower()

    # fallback: take last hex chunk in filename
    chunks = _RE_RUNID_ANYWHERE.findall(name)
    if not chunks:
        return None
    return chunks[-1].lower()


def _sorted_by_mtime_desc(paths: Iterable[Path]) -> List[Path]:
    return sorted(paths, key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)


def _split_keep(paths_sorted_desc: Sequence[Path], keep_n: int) -> Tuple[List[Path], List[Path]]:
    keep_n = max(0, int(keep_n))
    keep = list(paths_sorted_desc[:keep_n])
    drop = list(paths_sorted_desc[keep_n:])
    return keep, drop


def _age_days(path: Path, now_ts: float) -> float:
    try:
        return (now_ts - path.stat().st_mtime) / 86400.0
    except Exception:
        return 0.0


def _should_archive_by_age(path: Path, *, now_ts: float, max_age_days: Optional[int]) -> bool:
    if max_age_days is None:
        return True  # age filter disabled => archive allowed
    return _age_days(path, now_ts) > float(max_age_days)


def _move_file(src: Path, dst_dir: Path, *, dry_run: bool) -> Optional[Path]:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dry_run:
        return dst
    # If destination exists, make it unique
    if dst.exists():
        stem = dst.stem
        suffix = dst.suffix
        for i in range(1, 10_000):
            cand = dst_dir / f"{stem}__dup{i}{suffix}"
            if not cand.exists():
                dst = cand
                break
    shutil.move(str(src), str(dst))
    return dst


def _write_latest_pointers(*, logs_dir: Path, data_dir: Path) -> Dict[str, Any]:
    """
    Returns dict with {ok, run_id, latest_report, outputs...}
    """
    out: Dict[str, Any] = {"ok": False}

    run_reports = [
        p for p in logs_dir.glob("run_report_*.json")
        if p.is_file() and _is_run_report(p)
    ]
    run_reports = _sorted_by_mtime_desc(run_reports)
    if not run_reports:
        out["reason"] = "NO_RUN_REPORTS"
        return out

    latest_report = run_reports[0]
    run_id = _extract_run_id_from_run_report(latest_report)
    if not run_id:
        out["reason"] = "CANNOT_PARSE_RUN_ID"
        out["latest_report"] = str(latest_report)
        return out

    def _p(path: Path) -> Optional[str]:
        return str(path) if path.exists() else None

    latest_paths = {
        "schema": "latest_paths_v0",
        "ts_utc": _utc_now_z(),
        "run_id": run_id,
        "latest_report": str(latest_report),
        "events_run": _p(data_dir / f"events_run_{run_id}.jsonl"),
        "orders_paper": _p(data_dir / f"orders_paper_{run_id}.jsonl"),
        "orders_sendplan": _p(data_dir / f"orders_sendplan_{run_id}.jsonl"),
        "orders_exec": _p(data_dir / f"orders_exec_{run_id}.jsonl"),
        "sent_orders": _p(data_dir / f"sent_orders_{run_id}.jsonl"),
        "would_send": _p(data_dir / f"would_send_{run_id}.jsonl"),
    }

    _write_text(data_dir / "latest_run_id.txt", run_id + "\n")
    _write_json(data_dir / "latest_paths.json", latest_paths)

    out.update({"ok": True, "run_id": run_id, "latest_report": str(latest_report)})
    out["latest_paths"] = latest_paths
    return out


def _run_rotation(
    *,
    logs_dir: Path,
    data_dir: Path,
    cfg: RotateCfg,
    dry_run: bool,
) -> Dict[str, Any]:
    now_ts = time.time()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")

    # Archive base
    if cfg.archive_dir:
        arch_base = Path(cfg.archive_dir)
        if not arch_base.is_absolute():
            arch_base = logs_dir / cfg.archive_dir
    else:
        arch_base = logs_dir / "archive"

    arch_day = arch_base / today
    arch_logs = arch_day / "logs"
    arch_data = arch_day / "data"

    result: Dict[str, Any] = {
        "schema": _SCHEMA,
        "ts_utc": _utc_now_z(),
        "ok": True,
        "dry_run": bool(dry_run),
        "cfg": {
            "enabled": cfg.enabled,
            "keep_ops_logs": cfg.keep_ops_logs,
            "keep_run_reports": cfg.keep_run_reports,
            "keep_run_artifacts": cfg.keep_run_artifacts,
            "archive_dir": str(arch_base),
            "max_age_days": cfg.max_age_days,
        },
        "moved": {"logs": 0, "data": 0},
        "kept": {"ops_logs": 0, "run_reports": 0, "run_ids": 0},
        "errors": [],
        "archive_day": str(arch_day),
    }

    if not cfg.enabled:
        result["ok"] = True
        result["note"] = "rotation disabled by config"
        return result

    # 1) ops step logs (ONLY per-cycle logs, not ops_stage*.log etc)
    ops_step_logs = [p for p in logs_dir.glob("ops_*.log") if p.is_file() and _is_ops_step_log(p)]
    ops_step_logs = _sorted_by_mtime_desc(ops_step_logs)
    ops_keep, ops_drop = _split_keep(ops_step_logs, cfg.keep_ops_logs)
    result["kept"]["ops_logs"] = len(ops_keep)

    for p in ops_drop:
        if not _should_archive_by_age(p, now_ts=now_ts, max_age_days=cfg.max_age_days):
            continue
        try:
            _move_file(p, arch_logs, dry_run=dry_run)
            result["moved"]["logs"] += 1
        except Exception as e:
            result["ok"] = False
            result["errors"].append({"kind": "MOVE_FAIL", "path": str(p), "err": repr(e)})

    # 2) run reports
    run_reports = [p for p in logs_dir.glob("run_report_*.json") if p.is_file() and _is_run_report(p)]
    run_reports = _sorted_by_mtime_desc(run_reports)
    reports_keep, reports_drop = _split_keep(run_reports, cfg.keep_run_reports)
    result["kept"]["run_reports"] = len(reports_keep)

    # keep_run_ids: newest unique run ids from run reports (independent from keep_run_reports)
    keep_run_ids: Set[str] = set()
    for p in run_reports:
        rid = _extract_run_id_from_run_report(p)
        if not rid:
            continue
        if rid in keep_run_ids:
            continue
        keep_run_ids.add(rid)
        if len(keep_run_ids) >= cfg.keep_run_artifacts:
            break
    result["kept"]["run_ids"] = len(keep_run_ids)

    for p in reports_drop:
        if not _should_archive_by_age(p, now_ts=now_ts, max_age_days=cfg.max_age_days):
            continue
        try:
            _move_file(p, arch_logs, dry_run=dry_run)
            result["moved"]["logs"] += 1
        except Exception as e:
            result["ok"] = False
            result["errors"].append({"kind": "MOVE_FAIL", "path": str(p), "err": repr(e)})

    # 3) per-run data artifacts (allowlist prefixes only)
    candidates: List[Path] = []
    for prefix in _RUN_DATA_PREFIXES:
        candidates.extend([p for p in data_dir.glob(prefix + "*") if p.is_file()])

    # avoid duplicates
    seen: Set[str] = set()
    uniq: List[Path] = []
    for p in candidates:
        sp = str(p)
        if sp in seen:
            continue
        seen.add(sp)
        uniq.append(p)

    for p in uniq:
        rid = _extract_run_id_from_data_artifact(p)
        if not rid:
            continue
        if rid in keep_run_ids:
            continue
        if not _should_archive_by_age(p, now_ts=now_ts, max_age_days=cfg.max_age_days):
            continue
        try:
            _move_file(p, arch_data, dry_run=dry_run)
            result["moved"]["data"] += 1
        except Exception as e:
            result["ok"] = False
            result["errors"].append({"kind": "MOVE_FAIL", "path": str(p), "err": repr(e)})

    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m args.ops.rotate_logs_v0")
    parser.add_argument("--dry-run", action="store_true", help="Do not move files; only print what would happen.")
    parser.add_argument("--print-only", action="store_true", help="Alias for --dry-run (backward compat).")
    parser.add_argument("--control-state", default=None, help="Path to control_state.json (default: args/data/control_state.json)")
    args = parser.parse_args(argv)

    dry_run = bool(args.dry_run or args.print_only)

    # Resolve repo structure
    this = Path(__file__).resolve()
    args_dir = this.parents[1]  # .../args
    repo_root = this.parents[2]
    logs_dir = args_dir / "logs"
    data_dir = args_dir / "data"

    control_state_path = Path(args.control_state) if args.control_state else (data_dir / "control_state.json")
    cfg = _load_rotate_cfg(control_state_path)

    started = time.time()
    out: Dict[str, Any] = {
        "schema": _SCHEMA,
        "ts_utc": _utc_now_z(),
        "repo_root": str(repo_root),
        "args_dir": str(args_dir),
        "logs_dir": str(logs_dir),
        "data_dir": str(data_dir),
        "control_state_path": str(control_state_path),
        "dry_run": dry_run,
        "steps": [],
    }

    # Step A: latest pointers
    try:
        lp = _write_latest_pointers(logs_dir=logs_dir, data_dir=data_dir)
        out["steps"].append({"name": "latest_pointers", **lp})
    except Exception as e:
        out["steps"].append({"name": "latest_pointers", "ok": False, "err": repr(e)})

    # Step B: rotation
    try:
        rot = _run_rotation(logs_dir=logs_dir, data_dir=data_dir, cfg=cfg, dry_run=dry_run)
        out["steps"].append({"name": "rotation", **rot})
    except Exception as e:
        out["steps"].append({"name": "rotation", "ok": False, "err": repr(e)})

    out["duration_s"] = round(time.time() - started, 3)

    # overall ok if all steps ok
    ok = True
    errors_count = 0
    for s in out["steps"]:
        if not bool(s.get("ok", False)):
            ok = False
        if isinstance(s.get("errors"), list):
            errors_count += len(s["errors"])
    out["ok"] = ok
    out["errors_count"] = errors_count

    print(json.dumps(out, ensure_ascii=False, indent=2))

    # Exit policy:
    # - 0 if ok
    # - 2 if any step failed OR any move error happened
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
