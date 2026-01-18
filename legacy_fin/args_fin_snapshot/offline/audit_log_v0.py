from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from args.offline.evidence_history_v0 import find_repo_root, utc_iso, utc_now


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(txt: str) -> str:
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()


def _read_last_line(path: Path, max_bytes: int = 256 * 1024) -> Optional[str]:
    if not path.exists():
        return None
    size = path.stat().st_size
    if size <= 0:
        return None
    read = min(size, max_bytes)
    with path.open("rb") as f:
        f.seek(size - read)
        data = f.read(read)
    lines = data.splitlines()
    if not lines:
        return None
    if read < size and len(lines) > 1:
        lines = lines[1:]
    for b in reversed(lines):
        s = b.decode("utf-8", errors="replace").strip()
        if s:
            return s
    return None


class AuditLog:
    """Append-only JSONL with hash-chain (tamper-evident)."""

    def __init__(self, path: Path, *, kind: str):
        root = find_repo_root()
        self.path = path if path.is_absolute() else (root / path)
        self.kind = kind

    def _tail_state(self) -> Tuple[int, str]:
        last = _read_last_line(self.path)
        if not last:
            return 0, "0" * 64
        obj = json.loads(last)
        return int(obj.get("seq", 0) or 0), str(obj.get("hash") or "0" * 64)

    def append(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        actor: str,
        now_utc: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        last_seq, last_hash = self._tail_state()
        now = now_utc or utc_now()

        ev: Dict[str, Any] = {
            "kind": self.kind,
            "seq": last_seq + 1,
            "ts_utc": utc_iso(now),
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "prev_hash": last_hash,
        }
        ev["hash"] = _sha256(_canon(ev))
        line = _canon(ev)

        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return ev

    def verify(self) -> Tuple[bool, Dict[str, Any]]:
        if not self.path.exists():
            return True, {
                "ok": True,
                "records": 0,
                "note": "missing file (no events yet)",
            }

        exp_prev = "0" * 64
        exp_seq = 1
        records = 0

        with self.path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                records += 1

                if int(obj.get("seq", 0) or 0) != exp_seq:
                    return False, {
                        "ok": False,
                        "lineno": lineno,
                        "error": "seq mismatch",
                        "expected": exp_seq,
                        "got": obj.get("seq"),
                    }

                if str(obj.get("prev_hash") or "") != exp_prev:
                    return False, {
                        "ok": False,
                        "lineno": lineno,
                        "error": "prev_hash mismatch",
                    }

                got_hash = str(obj.get("hash") or "")
                obj2 = dict(obj)
                obj2.pop("hash", None)
                calc = _sha256(_canon(obj2))
                if got_hash != calc:
                    return False, {
                        "ok": False,
                        "lineno": lineno,
                        "error": "hash mismatch",
                    }

                exp_prev = got_hash
                exp_seq += 1

        return True, {
            "ok": True,
            "records": records,
            "last_seq": exp_seq - 1,
            "last_hash": exp_prev,
        }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ARGS audit log helper (v0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_v = sub.add_parser("verify")
    p_v.add_argument("--path", required=True)
    p_v.add_argument("--kind", default="model_registry_audit_v1")

    ns = ap.parse_args(argv)
    log = AuditLog(Path(ns.path), kind=ns.kind)
    ok, rep = log.verify()
    print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
