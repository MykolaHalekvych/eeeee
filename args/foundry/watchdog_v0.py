from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.foundry.guard_v0 import find_repo_root, require_engine_repo

EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2


def dump(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def read_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    return json.loads(raw)


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def list_releases(releases_dir: Path) -> List[Path]:
    if not releases_dir.exists():
        return []
    zips = sorted(releases_dir.glob("*.zip"), key=lambda p: p.name)
    return zips


def validate_release(zip_path: Path) -> Tuple[bool, List[str]]:
    """
    Validates:
      - hashes file exists: <release_id>.hashes.json
      - sha256(zip) matches hashes
      - zip is readable
    """
    errors: List[str] = []
    rid = zip_path.stem
    hashes_path = zip_path.with_suffix(".hashes.json")

    if not hashes_path.exists():
        errors.append("missing_release_hashes_json")
        return False, errors

    try:
        obj = read_json(hashes_path)
    except Exception as e:
        errors.append(f"release_hashes_load_failed:{type(e).__name__}")
        return False, errors

    if obj.get("schema") != "engine_release_hashes_v0":
        errors.append("release_hashes_bad_schema")
        return False, errors

    files = obj.get("files")
    if not isinstance(files, dict):
        errors.append("release_hashes_files_not_dict")
        return False, errors

    key = f"{rid}.zip"
    exp = files.get(key)
    if not isinstance(exp, str) or len(exp) < 16:
        errors.append("release_hashes_missing_zip_hash")
        return False, errors

    got = sha256_file(zip_path)
    if got != exp:
        errors.append("release_zip_hash_mismatch")
        return False, errors

    # zip readability
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # must contain at least 1 file
            names = zf.namelist()
            if not names:
                errors.append("release_zip_empty")
                return False, errors
            # quick read first member
            _ = zf.read(names[0])
    except Exception as e:
        errors.append(f"release_zip_read_failed:{type(e).__name__}")
        return False, errors

    return True, errors


def main() -> int:
    ap = argparse.ArgumentParser(prog="watchdog_v0")
    ap.add_argument("--releases-dir", default="dist/releases")
    ap.add_argument("--latest-only", action="store_true")
    args = ap.parse_args()

    repo_root = find_repo_root(Path("."))
    try:
        require_engine_repo(repo_root)
    except Exception as e:
        dump({"schema": "watchdog_v0", "ok": False, "exit_code": EXIT_INFRA, "error": str(e)})
        return EXIT_INFRA

    releases_dir = (repo_root / Path(args.releases_dir)).resolve()
    zips = list_releases(releases_dir)

    if not zips:
        dump(
            {
                "schema": "watchdog_v0",
                "ok": False,
                "exit_code": EXIT_EVAL_FAIL,
                "error": "no releases found",
                "releases_dir": str(releases_dir),
            }
        )
        return EXIT_EVAL_FAIL

    targets = [zips[-1]] if args.latest_only else zips

    results: List[Dict[str, Any]] = []
    ok_all = True
    for zp in targets:
        ok, errs = validate_release(zp)
        results.append(
            {
                "release_zip": str(zp),
                "ok": ok,
                "errors": errs,
            }
        )
        if not ok:
            ok_all = False

    code = EXIT_OK if ok_all else EXIT_EVAL_FAIL
    dump(
        {
            "schema": "watchdog_v0",
            "ok": ok_all,
            "exit_code": code,
            "releases_dir": str(releases_dir),
            "count": len(results),
            "results": results,
        }
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
