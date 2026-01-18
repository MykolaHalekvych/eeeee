from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2


def utc_now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(obj: Dict[str, Any], code: int) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    sys.exit(int(code))


def classify_exception(exc: Exception) -> Tuple[int, str]:
    if isinstance(exc, (FileNotFoundError, ValueError, zipfile.BadZipFile)):
        return RC_FAIL, "fail"
    if isinstance(exc, (PermissionError, OSError)):
        return RC_INFRA, "infra"
    return RC_INFRA, "infra"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    if tmp.exists():
        tmp.unlink(missing_ok=True)
    with tmp.open("wb") as f:
        f.write(data)
    if path.exists():
        path.unlink(missing_ok=True)
    tmp.replace(path)


def _safe_member_name(n: str) -> str:
    # release_pack_v0 writes flat basenames, but we enforce anyway
    if not n or n.endswith("/"):
        raise ValueError("bad_member_name")
    # normalize separators
    n2 = n.replace("\\", "/")
    base = Path(n2).name
    # disallow directories / traversal
    if base != n2:
        raise ValueError("member_not_flat")
    if base in ("", ".", ".."):
        raise ValueError("bad_member_basename")
    return base


def main_inner() -> Tuple[Dict[str, Any], int]:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--release-id", required=True)
    ap.add_argument("--releases-dir", default="")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--out-report", default="")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    releases_dir = (
        Path(args.releases_dir).resolve()
        if args.releases_dir.strip()
        else (repo / "dist" / "releases")
    )

    release_id = str(args.release_id).strip()
    release_zip = releases_dir / f"{release_id}.zip"

    out_dir = Path(args.out_dir).resolve()
    out_report = Path(args.out_report).resolve() if args.out_report.strip() else None

    if not release_zip.exists():
        raise FileNotFoundError(f"release_zip_missing:{release_zip}")

    # out_dir must be empty or not exist
    if out_dir.exists():
        # allow existing empty dir
        any_files = any(out_dir.iterdir())
        if any_files:
            raise ValueError(f"out_dir_not_empty:{out_dir}")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)

    extracted: List[str] = []
    dup: List[str] = []
    seen = set()

    with zipfile.ZipFile(str(release_zip), "r") as z:
        members = [m for m in z.namelist() if m and not m.endswith("/")]
        for m in members:
            base = _safe_member_name(m)
            if base.lower() in seen:
                dup.append(base)
                continue
            seen.add(base.lower())
            data = z.read(m)
            _atomic_write(out_dir / base, data)
            extracted.append(base)

    if dup:
        raise ValueError("duplicate_basenames_in_zip:" + ",".join(sorted(set(dup))))

    report = {
        "schema": "release_unpack_report_v1",
        "ts_utc": utc_now_iso(),
        "release_id": release_id,
        "release_zip": str(release_zip),
        "out_dir": str(out_dir),
        "files": sorted(extracted),
        "count": len(extracted),
    }

    if out_report is not None:
        out_report.parent.mkdir(parents=True, exist_ok=True)
        out_report.write_text(
            json.dumps(report, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    payload = {
        "schema": "release_unpack_v1",
        "ts_utc": utc_now_iso(),
        "ok": True,
        "exit_code": 0,
        "repo": str(repo),
        "release_id": release_id,
        "release_zip": str(release_zip),
        "out_dir": str(out_dir),
        "out_report": str(out_report) if out_report is not None else "",
        "count": len(extracted),
    }
    return payload, RC_OK


def main() -> None:
    try:
        payload, code = main_inner()
        emit(payload, code)
    except Exception as e:
        code, kind = classify_exception(e)
        emit(
            {
                "schema": "release_unpack_v1",
                "ts_utc": utc_now_iso(),
                "ok": False,
                "exit_code": code,
                "error": {"kind": kind, "type": type(e).__name__, "message": str(e)},
            },
            code,
        )


if __name__ == "__main__":
    main()
