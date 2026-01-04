from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA_VERSION = "soak_history_v0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _tail_last_line(path: Path) -> Optional[str]:
    try:
        if not path.exists():
            return None
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size <= 0:
                return None
            buf = b""
            pos = size
            while pos > 0 and b"\n" not in buf:
                step = 4096 if pos >= 4096 else pos
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
            lines = buf.splitlines()
            if not lines:
                return None
            return lines[-1].decode("utf-8", errors="replace")
    except Exception:
        return None


def _trim_to_last_n_lines(path: Path, max_lines: int) -> int:
    """
    Keeps last max_lines lines. Returns how many lines were trimmed (best-effort).
    """
    if max_lines <= 0:
        return 0
    try:
        if not path.exists():
            return 0
        dq: deque[str] = deque(maxlen=max_lines)
        total = 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                total += 1
                dq.append(line.rstrip("\n"))
        if total <= max_lines:
            return 0
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for line in dq:
                f.write(line + "\n")
        tmp.replace(path)
        return total - max_lines
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Append soak_status snapshots to an append-only JSONL history.")
    ap.add_argument("--in", dest="in_path", default="", help="Input soak_status.json (default args/data/soak_status.json)")
    ap.add_argument("--out", dest="out_path", default="", help="Output history JSONL (default args/logs/soak_history.jsonl)")
    ap.add_argument("--max-lines", type=int, default=20000, help="Trim history to last N lines (default 20000)")
    args = ap.parse_args()

    repo = _repo_root()
    in_path = Path(args.in_path) if args.in_path else (repo / "args" / "data" / "soak_status.json")
    out_path = Path(args.out_path) if args.out_path else (repo / "args" / "logs" / "soak_history.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    soak = _read_json(in_path)
    if not isinstance(soak, dict):
        obj = {
            "schema_version": SCHEMA_VERSION,
            "ts_utc": _iso_utc_now(),
            "ok": False,
            "exit_code": 2,
            "reason": "SOAK_STATUS_MISSING_OR_BAD_JSON",
            "in_path": str(in_path),
            "out_path": str(out_path),
        }
        sys.stdout.write(json.dumps(obj, ensure_ascii=False))
        return 2

    prev_sha = None
    last_line = _tail_last_line(out_path)
    if last_line:
        try:
            last_obj = json.loads(last_line)
            prev_sha = last_obj.get("sha256")
        except Exception:
            prev_sha = None

    # Canonical content (sha computed over this)
    core = {
        "kind": "SOAK_SNAPSHOT",
        "schema_version": SCHEMA_VERSION,
        "ts_utc": soak.get("ts_utc") or _iso_utc_now(),
        "prev_sha256": prev_sha,
        "soak": soak,
    }
    core_str = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sha = _sha256(core_str)

    entry = dict(core)
    entry["sha256"] = sha

    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    trimmed = _trim_to_last_n_lines(out_path, args.max_lines)

    out = {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _iso_utc_now(),
        "ok": True,
        "exit_code": 0,
        "reason": "APPENDED",
        "in_path": str(in_path),
        "out_path": str(out_path),
        "sha256": sha,
        "prev_sha256": prev_sha,
        "trimmed_lines": trimmed,
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
