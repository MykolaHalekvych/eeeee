from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.foundry.guard_v0 import find_repo_root, require_engine_repo

EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2


def dump(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def list_release_pairs(releases_dir: Path) -> List[Tuple[Path, Path]]:
    zips = sorted(releases_dir.glob("*.zip"), key=lambda p: p.name)
    pairs: List[Tuple[Path, Path]] = []
    for zp in zips:
        hp = zp.with_suffix(".hashes.json")
        pairs.append((zp, hp))
    return pairs


def file_mtime_utc(p: Path) -> datetime:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)


def decide_deletions(pairs: List[Tuple[Path, Path]], keep_last: int, max_age_days: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Keep strategy:
      - keep newest N zips always
      - delete anything older than max_age_days (zip + its hashes if present), excluding newest N
    """
    items: List[Dict[str, Any]] = []
    for zp, hp in pairs:
        m = file_mtime_utc(zp)
        items.append({
            "zip": zp,
            "hash": hp,
            "mtime_utc": m,
            "mtime_iso": m.isoformat().replace("+00:00", "Z"),
        })

    # newest first
    items_sorted = sorted(items, key=lambda x: x["mtime_utc"], reverse=True)
    keep = items_sorted[: max(0, keep_last)]
    candidates = items_sorted[max(0, keep_last):]

    cutoff = utc_now() - timedelta(days=max_age_days)
    to_delete: List[Dict[str, Any]] = []
    to_keep: List[Dict[str, Any]] = []

    for x in keep:
        to_keep.append({
            "zip": str(x["zip"]),
            "hash": str(x["hash"]),
            "mtime_iso": x["mtime_iso"],
            "reason": "keep_last",
        })

    for x in candidates:
        if x["mtime_utc"] < cutoff:
            to_delete.append({
                "zip": str(x["zip"]),
                "hash": str(x["hash"]),
                "mtime_iso": x["mtime_iso"],
                "reason": "older_than_cutoff",
                "cutoff_iso": cutoff.isoformat().replace("+00:00", "Z"),
            })
        else:
            to_keep.append({
                "zip": str(x["zip"]),
                "hash": str(x["hash"]),
                "mtime_iso": x["mtime_iso"],
                "reason": "within_ttl",
            })

    return to_keep, to_delete


def safe_unlink(p: Path) -> None:
    if p.exists() and p.is_file():
        p.unlink()


def main() -> int:
    ap = argparse.ArgumentParser(prog="housekeeping_v0")
    ap.add_argument("--releases-dir", default="dist/releases")
    ap.add_argument("--keep-last", type=int, default=3)
    ap.add_argument("--max-age-days", type=int, default=30)
    ap.add_argument("--confirm-delete", default="NO", help="set to YES to delete")
    args = ap.parse_args()

    repo_root = find_repo_root(Path("."))
    try:
        require_engine_repo(repo_root)
    except Exception as e:
        dump({"schema": "housekeeping_v0", "ok": False, "exit_code": EXIT_INFRA, "error": str(e)})
        return EXIT_INFRA

    releases_dir = (repo_root / Path(args.releases_dir)).resolve()
    if not releases_dir.exists():
        dump({
            "schema": "housekeeping_v0",
            "ok": True,
            "exit_code": 0,
            "releases_dir": str(releases_dir),
            "note": "no releases dir",
            "dryrun": True,
            "deleted": 0,
        })
        return EXIT_OK

    pairs = list_release_pairs(releases_dir)
    to_keep, to_delete = decide_deletions(pairs, args.keep_last, args.max_age_days)

    dryrun = (str(args.confirm_delete).strip().upper() != "YES")
    deleted = 0
    errors: List[str] = []

    if not dryrun:
        for x in to_delete:
            try:
                zp = Path(x["zip"])
                hp = Path(x["hash"])
                safe_unlink(zp)
                safe_unlink(hp)
                deleted += 1
            except Exception as e:
                errors.append(f"delete_failed:{type(e).__name__}:{e}")

    ok = len(errors) == 0
    code = EXIT_OK if ok else EXIT_INFRA

    dump({
        "schema": "housekeeping_v0",
        "ok": ok,
        "exit_code": code,
        "releases_dir": str(releases_dir),
        "dryrun": dryrun,
        "policy": {
            "keep_last": args.keep_last,
            "max_age_days": args.max_age_days,
        },
        "kept": to_keep,
        "planned_delete": to_delete,
        "deleted": deleted,
        "errors": errors[:10],
    })
    return code


if __name__ == "__main__":
    raise SystemExit(main())
