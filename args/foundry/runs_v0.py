from __future__ import annotations

import argparse
import json
import platform
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


SCHEMA_RUN_META = "args.foundry.runs_v0.run_meta.v0"
SCHEMA_EVENT = "args.foundry.runs_v0.event.v0"
SCHEMA_FINAL_REPORT = "args.foundry.runs_v0.final_report.v0"

DEFAULT_GUARD_FILENAME = ".args_engine_repo"
STEP_EVENTS = {"GATE", "PLAN", "BUILD", "RELEASE"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _git_cmd(repo_root: Path, args: list[str]) -> str | None:
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None

    if cp.returncode != 0:
        return None

    out = (cp.stdout or "").strip()
    return out or None


def git_short_sha(repo_root: Path) -> str:
    return _git_cmd(repo_root, ["rev-parse", "--short", "HEAD"]) or "nogit"


def git_full_sha(repo_root: Path) -> str | None:
    return _git_cmd(repo_root, ["rev-parse", "HEAD"])


def git_is_dirty(repo_root: Path) -> bool | None:
    out = _git_cmd(repo_root, ["status", "--porcelain"])
    if out is None:
        return None
    return bool(out.strip())


def find_repo_root(
    start_dir: Path, guard_filename: str = DEFAULT_GUARD_FILENAME
) -> Path:
    cur = start_dir.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / guard_filename).is_file():
            return candidate
    return cur


def new_run_id(repo_root: Path) -> str:
    ts = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    sha = git_short_sha(repo_root)
    rnd = secrets.token_hex(4)  # 8 hex chars
    return f"{ts}__{sha}__{rnd}"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def append_event(
    run_dir: Path,
    event: str,
    status: str,
    *,
    exit_code: int | None = None,
    data: dict[str, Any] | None = None,
) -> Path:
    ensure_dir(run_dir)
    record: dict[str, Any] = {
        "schema": SCHEMA_EVENT,
        "ts": _utc_now_iso(),
        "event": str(event).upper(),
        "status": str(status).upper(),
    }
    if exit_code is not None:
        record["exit_code"] = int(exit_code)
    if data:
        record["data"] = data

    events_path = run_dir / "events.jsonl"
    with events_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(_json_dumps(record))
        f.write("\n")

    return events_path


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _safe_load_json(path: Path) -> Any | None:
    try:
        return _load_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_run_meta(
    run_dir: Path,
    *,
    run_id: str,
    repo_root: Path,
    product: str | None,
    factory: str | None,
    control_plane: str | None,
) -> Path:
    meta_path = run_dir / "run_meta.json"
    meta: dict[str, Any] = {
        "schema": SCHEMA_RUN_META,
        "run_id": run_id,
        "created_at": _utc_now_iso(),
        "repo_root": str(repo_root.resolve()),
        "product": product,
        "factory": factory,
        "control_plane": control_plane,
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "git": {
            "short_sha": git_short_sha(repo_root),
            "full_sha": git_full_sha(repo_root),
            "dirty": git_is_dirty(repo_root),
        },
    }
    ensure_dir(run_dir)
    meta_path.write_text(_json_dumps(meta) + "\n", encoding="utf-8", newline="\n")
    return meta_path


def _iter_events(events_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not events_path.exists():
        return events
    with events_path.open("r", encoding="utf-8") as f:
        for line in f:
            ln = line.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _artifact_info(repo_root: Path, label: str, raw_path: str) -> dict[str, Any]:
    p = Path(raw_path)
    abs_path = p if p.is_absolute() else (repo_root / p)
    abs_path_resolved = abs_path.resolve()
    exists = abs_path_resolved.exists()

    info: dict[str, Any] = {
        "label": label,
        "path": raw_path,
        "abs_path": str(abs_path_resolved),
        "exists": exists,
    }
    if exists and abs_path_resolved.is_file():
        info["size_bytes"] = abs_path_resolved.stat().st_size
        info["sha256"] = _sha256_file(abs_path_resolved)
    return info


def _infer_overall_status(step_events: list[dict[str, Any]]) -> str:
    statuses = [str(e.get("status") or "").upper() for e in step_events]
    if any(s == "HALT" for s in statuses):
        return "HALT"
    if not statuses:
        return "UNKNOWN"
    if any(s not in {"PASS", "OK", "SKIP"} for s in statuses):
        return "FAIL"
    return "PASS"


def _infer_overall_exit_code(overall_status: str) -> int:
    if overall_status == "PASS":
        return 0
    if overall_status == "HALT":
        return 2
    return 1


def finalize(
    run_dir: Path,
    *,
    overall_status: str | None = None,
    overall_exit_code: int | None = None,
    seed: dict[str, Any] | None = None,
) -> Path:
    ensure_dir(run_dir)

    meta = _safe_load_json(run_dir / "run_meta.json") or {}
    seed = seed or {}

    if isinstance(meta, dict) and meta.get("repo_root"):
        repo_root = Path(str(meta["repo_root"]))
    elif seed.get("repo_root"):
        repo_root = Path(str(seed["repo_root"]))
    else:
        repo_root = find_repo_root(run_dir)

    events_path = run_dir / "events.jsonl"
    events_before_end = _iter_events(events_path)
    step_events_before_end = [
        e for e in events_before_end if e.get("event") in STEP_EVENTS
    ]

    if overall_status is None:
        overall_status = _infer_overall_status(step_events_before_end)
    overall_status = str(overall_status).upper()

    if overall_exit_code is None:
        overall_exit_code = _infer_overall_exit_code(overall_status)

    append_event(run_dir, "END", overall_status, exit_code=overall_exit_code)

    events = _iter_events(events_path)
    step_events = [e for e in events if e.get("event") in STEP_EVENTS]

    artifacts: list[dict[str, Any]] = []
    raw_artifacts = seed.get("artifacts")
    if isinstance(raw_artifacts, list):
        for a in raw_artifacts:
            if not isinstance(a, dict):
                continue
            label = str(a.get("label") or "").strip()
            raw_path = str(a.get("path") or "").strip()
            if not label or not raw_path:
                continue
            artifacts.append(_artifact_info(repo_root, label, raw_path))

    steps_last: dict[str, dict[str, Any]] = {}
    for e in step_events:
        ev_name = str(e.get("event") or "")
        if ev_name:
            steps_last[ev_name] = e

    report: dict[str, Any] = {
        "schema": SCHEMA_FINAL_REPORT,
        "run_id": meta.get("run_id"),
        "run_dir": str(run_dir.resolve()),
        "created_at": _utc_now_iso(),
        "repo": {
            "root": str(repo_root.resolve()),
            "git": meta.get("git"),
        },
        "context": {
            "product": meta.get("product") or seed.get("product"),
            "factory": meta.get("factory") or seed.get("factory"),
            "control_plane": meta.get("control_plane") or seed.get("control_plane"),
            "window": seed.get("window"),
        },
        "paths": {
            "run_meta": str((run_dir / "run_meta.json").resolve()),
            "events": str(events_path.resolve()),
            "final_report": str((run_dir / "final_report.json").resolve()),
        },
        "events": {
            "count": len(events),
            "first_ts": events[0].get("ts") if events else None,
            "last_ts": events[-1].get("ts") if events else None,
        },
        "steps": {
            "ordered": step_events,
            "last": steps_last,
        },
        "artifacts": artifacts,
        "overall": {
            "status": overall_status,
            "exit_code": int(overall_exit_code),
        },
        "seed": seed,
    }

    report_path = run_dir / "final_report.json"
    report_path.write_text(_json_dumps(report) + "\n", encoding="utf-8", newline="\n")
    return report_path


def _parse_json_arg(data_json: str | None, data_file: str | None) -> dict[str, Any]:
    if data_file:
        try:
            obj = _load_json(Path(data_file))
        except Exception:
            return {}
        return obj if isinstance(obj, dict) else {}
    if not data_json:
        return {}
    try:
        obj = json.loads(data_json)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="args.foundry.runs_v0")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new-run-id", help="Generate a new run_id.")
    p_new.add_argument("--repo-root", default=".", help="Repo root for git short SHA.")

    p_init = sub.add_parser("init", help="Initialize a run dir and write START event.")
    p_init.add_argument("--run-dir", required=True)
    p_init.add_argument("--run-id", required=True)
    p_init.add_argument("--repo-root", default=".")
    p_init.add_argument("--product")
    p_init.add_argument("--factory")
    p_init.add_argument("--control-plane")

    p_event = sub.add_parser("event", help="Append one event to events.jsonl.")
    p_event.add_argument("--run-dir", required=True)
    p_event.add_argument("--event", required=True)
    p_event.add_argument("--status", required=True)
    p_event.add_argument("--exit-code", type=int)
    p_event.add_argument("--data-json")
    p_event.add_argument("--data-file")

    p_final = sub.add_parser("finalize", help="Append END and write final_report.json.")
    p_final.add_argument("--run-dir", required=True)
    p_final.add_argument("--overall-status")
    p_final.add_argument("--overall-exit-code", type=int)
    p_final.add_argument("--seed-file")

    args = parser.parse_args(argv)

    if args.cmd == "new-run-id":
        repo_root = Path(args.repo_root)
        sys.stdout.write(new_run_id(repo_root) + "\n")
        return 0

    if args.cmd == "init":
        run_dir = Path(args.run_dir)
        repo_root = Path(args.repo_root)
        write_run_meta(
            run_dir,
            run_id=str(args.run_id),
            repo_root=repo_root,
            product=args.product,
            factory=args.factory,
            control_plane=args.control_plane,
        )
        append_event(
            run_dir,
            "START",
            "OK",
            data={
                "run_id": str(args.run_id),
                "product": args.product,
                "factory": args.factory,
            },
        )
        return 0

    if args.cmd == "event":
        run_dir = Path(args.run_dir)
        data = _parse_json_arg(args.data_json, args.data_file)
        append_event(
            run_dir,
            str(args.event),
            str(args.status),
            exit_code=args.exit_code,
            data=data or None,
        )
        return 0

    if args.cmd == "finalize":
        run_dir = Path(args.run_dir)
        seed = _safe_load_json(Path(args.seed_file)) if args.seed_file else None
        finalize(
            run_dir,
            overall_status=args.overall_status,
            overall_exit_code=args.overall_exit_code,
            seed=seed if isinstance(seed, dict) else None,
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
