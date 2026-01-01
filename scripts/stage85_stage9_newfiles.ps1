# scripts/stage85_stage9_newfiles.ps1
# Creates Stage 8.5 + Stage 9 new files without git patch.
# Safe: only writes text files under args\offline and args\process

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

# --- args/offline/evidence_history_v0.py
Write-Utf8NoBom "args/offline/evidence_history_v0.py" @'
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def find_repo_root(start: Optional[Path] = None) -> Path:
    p = (start or Path(__file__)).resolve()
    for parent in [p] + list(p.parents):
        if (parent / "args").is_dir():
            return parent
    return Path.cwd()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def utc_compact(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_json_tolerant(path: Path) -> Any:
    txt = path.read_text(encoding="utf-8").lstrip("\ufeff \t\r\n")
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(txt)
    return obj


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return str(path)


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def record_json_evidence(
    *,
    kind: str,
    report_obj: Dict[str, Any],
    evidence_dir: Path,
    report_basename: str,
    latest_pointer_path: Optional[Path] = None,
    now_utc: Optional[datetime] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    root = repo_root or find_repo_root()
    now = now_utc or utc_now()
    ts = utc_compact(now)

    ev_dir = evidence_dir if evidence_dir.is_absolute() else (root / evidence_dir)
    ev_dir.mkdir(parents=True, exist_ok=True)

    report_path = ev_dir / f"{ts}_{report_basename}"
    atomic_write_json(report_path, report_obj)

    digest = sha256_file(report_path)
    st = report_path.stat()

    ptr: Dict[str, Any] = {
        "kind": f"{kind}_evidence_latest_pointer_v1",
        "updated_at_utc": utc_iso(now),
        "latest_report_path": _safe_rel(report_path, root),
        "latest_report_sha256": digest,
        "latest_report_bytes": st.st_size,
    }

    if isinstance(report_obj.get("status"), str):
        ptr["latest_status"] = report_obj["status"]
    if isinstance(report_obj.get("model_id"), str):
        ptr["model_id"] = report_obj["model_id"]

    lp = latest_pointer_path or (ev_dir / "latest.json")
    lp_abs = lp if lp.is_absolute() else (root / lp)
    atomic_write_json(lp_abs, ptr)
    return ptr


def verify_latest_pointer(latest_pointer_path: Path, *, repo_root: Optional[Path] = None) -> Tuple[bool, str]:
    root = repo_root or find_repo_root()
    lp = latest_pointer_path if latest_pointer_path.is_absolute() else (root / latest_pointer_path)
    if not lp.exists():
        return False, f"latest pointer not found: {lp}"

    ptr = load_json_tolerant(lp)
    if not isinstance(ptr, dict):
        return False, "latest pointer JSON is not an object"

    rp = ptr.get("latest_report_path")
    if not isinstance(rp, str) or not rp.strip():
        return False, "latest_report_path missing/invalid"

    report_abs = (root / rp) if not Path(rp).is_absolute() else Path(rp)
    if not report_abs.exists():
        return False, f"latest_report_path does not exist: {report_abs}"

    expected = ptr.get("latest_report_sha256")
    if not isinstance(expected, str) or len(expected) < 32:
        return False, "latest_report_sha256 missing/invalid"

    got = sha256_file(report_abs)
    if got.lower() != expected.lower():
        return False, f"SHA256 mismatch: expected {expected}, got {got}"

    return True, "OK"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ARGS offline evidence history helper (v0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_v = sub.add_parser("verify", help="Verify latest pointer + report SHA256")
    p_v.add_argument("--latest_pointer", required=True)

    p_st = sub.add_parser("selftest", help="Create dummy evidence and verify it")
    p_st.add_argument("--evidence_dir", default="args/offline/evidence/_selftest_eval_gate")

    ns = ap.parse_args(argv)

    if ns.cmd == "verify":
        ok, msg = verify_latest_pointer(Path(ns.latest_pointer))
        print(msg)
        return 0 if ok else 2

    if ns.cmd == "selftest":
        sample = {"kind": "eval_gate_report_v0", "status": "PASS", "model_id": "selftest"}
        record_json_evidence(kind="eval_gate", report_obj=sample, evidence_dir=Path(ns.evidence_dir), report_basename="eval_gate_report.json")
        ok, msg = verify_latest_pointer(Path(ns.evidence_dir) / "latest.json")
        print(msg)
        return 0 if ok else 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
'@

# --- args/offline/audit_log_v0.py
Write-Utf8NoBom "args/offline/audit_log_v0.py" @'
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

    def append(self, event_type: str, payload: Dict[str, Any], *, actor: str, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
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
            return True, {"ok": True, "records": 0, "note": "missing file (no events yet)"}

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
                    return False, {"ok": False, "lineno": lineno, "error": "seq mismatch", "expected": exp_seq, "got": obj.get("seq")}

                if str(obj.get("prev_hash") or "") != exp_prev:
                    return False, {"ok": False, "lineno": lineno, "error": "prev_hash mismatch"}

                got_hash = str(obj.get("hash") or "")
                obj2 = dict(obj)
                obj2.pop("hash", None)
                calc = _sha256(_canon(obj2))
                if got_hash != calc:
                    return False, {"ok": False, "lineno": lineno, "error": "hash mismatch"}

                exp_prev = got_hash
                exp_seq += 1

        return True, {"ok": True, "records": records, "last_seq": exp_seq - 1, "last_hash": exp_prev}


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
'@

# --- args/process package
Write-Utf8NoBom "args/process/__init__.py" @'
"""ARGS Process Pack (Stage 9)."""
'@

Write-Utf8NoBom "args/process/PROTOCOL_PATCH_PROVE_RELEASE.md" @'
# Stage 9 — Process Pack
## Patch → Prove → Release protocol (offline-learning only)

Hard rules:
- No runtime learning (training only OFFLINE between iterations)
- Every release must have: eval evidence history + registry audit + forensic report + git tag

See README section in your repo for operational usage.
'@

Write-Host "DONE."
