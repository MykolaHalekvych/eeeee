#!/usr/bin/env python3
"""
args.ops.roll_ops_events_v0

Roll/truncate args/logs/ops_events.jsonl by size while preserving tail lines.

Design goals:
- Keep soak_check_v0 compatibility (it reads ops_events.jsonl).
- Avoid unbounded growth of ops_events.jsonl in 24x7 ops.
- Best-effort, count/size based. Archive full file before truncation.

Algorithm (when enabled and file exceeds max_bytes):
  1) Read last keep_tail_lines into memory (deque).
  2) Move original ops_events.jsonl into archive file.
  3) Write new ops_events.jsonl with tail lines.

Config:
- Read from args/data/control_state.json:
  control_state["ops_loop"]["events_roll"] = {
    "enabled": true,
    "max_bytes": 50_000_000,
    "keep_tail_lines": 20000,
    "archive_dir": "archive/ops_events"   # relative to args/logs/
  }

Exit codes:
- 0 OK / NOOP
- 2 error
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence


def _utc_now_z() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class EventsRollCfg:
    enabled: bool = True
    max_bytes: int = 50_000_000
    keep_tail_lines: int = 20_000
    archive_dir: str = "archive/ops_events"  # relative to args/logs/


def _load_cfg(control_state_path: Path) -> EventsRollCfg:
    cfg = EventsRollCfg()
    if not control_state_path.exists():
        return cfg

    try:
        d = _read_json(control_state_path)
    except Exception:
        return cfg

    ops_loop = d.get("ops_loop") or {}
    roll = ops_loop.get("events_roll") or {}
    if not isinstance(roll, dict):
        return cfg

    def _get_int(key: str, default: int) -> int:
        v = roll.get(key, default)
        try:
            return int(v)
        except Exception:
            return default

    enabled = bool(roll.get("enabled", cfg.enabled))
    max_bytes = _get_int("max_bytes", cfg.max_bytes)
    keep_tail_lines = _get_int("keep_tail_lines", cfg.keep_tail_lines)
    archive_dir = str(roll.get("archive_dir", cfg.archive_dir))

    return EventsRollCfg(
        enabled=enabled,
        max_bytes=max(1_000_000, max_bytes),         # guardrail: 1MB+
        keep_tail_lines=max(100, keep_tail_lines),   # guardrail: >=100 lines
        archive_dir=archive_dir,
    )


def _read_tail_lines(path: Path, keep_tail_lines: int) -> deque[str]:
    tail: deque[str] = deque(maxlen=keep_tail_lines)
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            tail.append(line)
    return tail


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m args.ops.roll_ops_events_v0")
    parser.add_argument("--dry-run", action="store_true", help="Do not move/write; only report.")
    parser.add_argument("--control-state", default=None, help="Path to control_state.json (default: args/data/control_state.json)")
    args = parser.parse_args(argv)

    dry_run = bool(args.dry_run)

    # Resolve repo layout
    this = Path(__file__).resolve()
    args_dir = this.parents[1]  # .../args
    logs_dir = args_dir / "logs"
    data_dir = args_dir / "data"

    control_state_path = Path(args.control_state) if args.control_state else (data_dir / "control_state.json")
    cfg = _load_cfg(control_state_path)

    events_path = logs_dir / "ops_events.jsonl"

    started = time.time()
    out: Dict[str, Any] = {
        "schema": "roll_ops_events_v0",
        "ts_utc": _utc_now_z(),
        "ok": True,
        "dry_run": dry_run,
        "events_path": str(events_path),
        "control_state_path": str(control_state_path),
        "cfg": {
            "enabled": cfg.enabled,
            "max_bytes": cfg.max_bytes,
            "keep_tail_lines": cfg.keep_tail_lines,
            "archive_dir": cfg.archive_dir,
        },
        "action": "NOOP",
        "details": {},
        "errors": [],
    }

    try:
        if not cfg.enabled:
            out["action"] = "DISABLED"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        if not events_path.exists():
            out["action"] = "NO_EVENTS_FILE"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        size = events_path.stat().st_size
        out["details"]["size_bytes"] = size

        if size <= cfg.max_bytes:
            out["action"] = "UNDER_LIMIT"
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        # Prepare archive path
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        day = datetime.now(timezone.utc).strftime("%Y%m%d")

        arch_base = Path(cfg.archive_dir)
        if not arch_base.is_absolute():
            arch_base = logs_dir / arch_base

        arch_dir = arch_base / day
        arch_path = arch_dir / f"ops_events_{ts}.jsonl"

        out["action"] = "ROLL"
        out["details"].update({
            "archive_path": str(arch_path),
            "archive_dir": str(arch_dir),
        })

        # Read tail before moving
        tail = _read_tail_lines(events_path, cfg.keep_tail_lines)
        out["details"]["tail_lines"] = len(tail)

        if dry_run:
            out["details"]["would_move"] = True
            out["details"]["would_write_new_tail"] = True
        else:
            arch_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(events_path), str(arch_path))

            # Write new events file with tail lines
            with events_path.open("w", encoding="utf-8", newline="\n") as f:
                for line in tail:
                    if line.endswith("\n"):
                        f.write(line)
                    else:
                        f.write(line + "\n")

            out["details"]["new_size_bytes"] = events_path.stat().st_size

        out["duration_s"] = round(time.time() - started, 3)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    except Exception as e:
        out["ok"] = False
        out["errors"].append({"err": repr(e)})
        out["duration_s"] = round(time.time() - started, 3)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
