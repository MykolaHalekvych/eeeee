from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def iter_jsonl(path: Path) -> Iterable[Tuple[int, Dict[str, Any]]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield line_no, obj


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


_VOLATILE_FALLBACK_KEYS = {"ts", "timestamp", "created_at", "updated_at"}


def event_key(ev: Dict[str, Any]) -> str:
    for k in ("event_id", "event_uid", "id"):
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return f"{k}:{v.strip()}"
    ev2 = {k: v for k, v in ev.items() if k not in _VOLATILE_FALLBACK_KEYS}
    return "sha1:" + _sha1_hex(_canonical(ev2))


@dataclass(frozen=True)
class MergeResult:
    dst_existing: int
    src_total: int
    appended: int
    skipped_dupe: int


def merge_events_jsonl(dst_path: Path, src_path: Path) -> MergeResult:
    # build seen keys from dst
    seen = set()
    dst_existing = 0
    for _, ev in iter_jsonl(dst_path):
        dst_existing += 1
        seen.add(event_key(ev))

    src_total = 0
    appended = 0
    skipped = 0
    for _, ev in iter_jsonl(src_path):
        src_total += 1
        k = event_key(ev)
        if k in seen:
            skipped += 1
            continue
        append_jsonl(dst_path, ev)
        seen.add(k)
        appended += 1

    return MergeResult(
        dst_existing=dst_existing,
        src_total=src_total,
        appended=appended,
        skipped_dupe=skipped,
    )
