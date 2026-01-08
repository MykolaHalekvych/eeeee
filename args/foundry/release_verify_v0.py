from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCHEMA = "release_verify_v0"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_stream(f) -> str:
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def emit_and_exit(payload: Dict[str, Any], code: int) -> int:
    payload["schema"] = payload.get("schema", SCHEMA)
    payload["ts_utc"] = payload.get("ts_utc", utc_ts())
    payload["exit_code"] = int(code)
    payload["ok"] = (int(code) == 0)
    s = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write(s)
    sys.stdout.flush()
    return int(code)


def classify_exit_code(exc: Exception) -> int:
    # FAIL(1): missing input / bad format / bad zip
    if isinstance(exc, (FileNotFoundError, ValueError, zipfile.BadZipFile)):
        return 1
    # INFRA(2): permissions, OS errors, unexpected runtime issues
    return 2


def parse_product_id_from_release_id(release_id: str) -> str:
    # Expected: <product_id>__vX.Y.Z__<hash>
    # If format differs, fall back to leftmost token.
    if "__v" in release_id:
        return release_id.split("__v", 1)[0]
    if "__" in release_id:
        return release_id.split("__", 1)[0]
    return release_id


def run_exe_help(exe_path: Path) -> Tuple[Dict[str, Any], bool]:
    """
    Returns (exec_res, infra_flag).
    infra_flag=True only when OS-level execution failed (e.g., OSError 22).
    """
    try:
        p = subprocess.run([str(exe_path), "--help"], capture_output=True, text=True, check=False)
        return (
            {
                "skipped": False,
                "rc": int(p.returncode),
                "stdout": p.stdout or "",
                "stderr": p.stderr or "",
                "path": str(exe_path),
            },
            False,
        )
    except OSError as e:
        return (
            {
                "skipped": False,
                "rc": -1,
                "stdout": "",
                "stderr": "",
                "path": str(exe_path),
                "error": {"kind": e.__class__.__name__, "message": str(e)},
            },
            True,
        )


def main_inner() -> Tuple[Dict[str, Any], int]:
    ap = argparse.ArgumentParser(description="Verify release zip integrity (integrity-only by default)")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--release-id", required=True)
    ap.add_argument("--releases-dir", default=None)
    ap.add_argument("--run-exe-check", default="NO", choices=["YES", "NO"])
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    releases_dir = Path(args.releases_dir).resolve() if args.releases_dir else (repo / "dist" / "releases")

    release_zip = releases_dir / f"{args.release_id}.zip"
    release_hashes = releases_dir / f"{args.release_id}.hashes.json"

    if not release_zip.exists():
        raise FileNotFoundError(f"release zip not found: {release_zip}")
    if not release_hashes.exists():
        raise FileNotFoundError(f"release hashes not found: {release_hashes}")

    rh = read_json(release_hashes)
    errors: List[str] = []
    infra = False

    # Zip artifact sha256 (outer zip file)
    zip_sha = sha256_file(release_zip)
    expected_zip_sha = (((rh.get("artifacts") or {}).get("release_zip") or {}).get("sha256"))
    if expected_zip_sha and str(expected_zip_sha).lower() != zip_sha.lower():
        errors.append("zip sha256 mismatch")

    included_files = rh.get("included_files")
    file_sha256 = rh.get("file_sha256")

    verify_mode = "legacy_5_files"
    expected_files: List[str] = ["app.exe", "config.example.json", "evidence.md", "hashes.json", "runbook.md"]

    if isinstance(included_files, list) and isinstance(file_sha256, dict):
        verify_mode = "manifest_superset"
        expected_files = sorted([str(x) for x in included_files])

    # Open zip, list members (root basenames), and verify content + hashes without extraction
    zip_members_by_base: Dict[str, str] = {}
    duplicate_basenames: List[str] = []

    try:
        with zipfile.ZipFile(str(release_zip), "r") as z:
            names = [n for n in z.namelist() if n and not n.endswith("/")]
            for full in names:
                base = Path(full).name
                if base in zip_members_by_base:
                    duplicate_basenames.append(base)
                else:
                    zip_members_by_base[base] = full

            if duplicate_basenames:
                errors.append("zip contains duplicate basenames: " + ",".join(sorted(set(duplicate_basenames))))

            zip_files = sorted(zip_members_by_base.keys())

            # Content check
            if verify_mode == "manifest_superset":
                expected_set = set(expected_files)
                got_set = set(zip_files)
                if expected_set != got_set:
                    errors.append(f"zip content mismatch: expected {sorted(expected_set)} got {sorted(got_set)}")
            else:
                if zip_files != expected_files:
                    errors.append(f"zip content mismatch: expected {expected_files} got {zip_files}")

            # Hash check (only when manifest provides file_sha256)
            if verify_mode == "manifest_superset" and isinstance(file_sha256, dict):
                for name in expected_files:
                    expected_sha = file_sha256.get(name)
                    if not expected_sha:
                        errors.append(f"missing expected sha256 for {name} in release_hashes")
                        continue
                    member = zip_members_by_base.get(name)
                    if not member:
                        errors.append(f"missing file in zip: {name}")
                        continue
                    try:
                        with z.open(member, "r") as f:
                            actual = sha256_stream(f)
                        if str(expected_sha).lower() != actual.lower():
                            errors.append(f"sha256 mismatch for {name}")
                    except Exception as e:  # noqa: BLE001
                        # treat unexpected zip read errors as INFRA
                        errors.append(f"zip read/hash error for {name}: {e.__class__.__name__}")
                        infra = True

    except zipfile.BadZipFile:
        # FAIL(1): bad zip is product/artifact failure, not infra
        errors.append("zip bad file")
        zip_files = []
    except Exception as e:  # noqa: BLE001
        # INFRA(2): permissions, IO errors opening zip
        errors.append(f"zip open error: {e.__class__.__name__}")
        zip_files = []
        infra = True

    # EXE check (opt-in only)
    exec_res: Dict[str, Any] = {"skipped": True, "rc": 0, "stdout": "", "stderr": ""}
    if args.run_exe_check == "YES":
        product_id = parse_product_id_from_release_id(args.release_id)
        dist_exe = repo / "dist" / product_id / "app.exe"
        exec_res = {"skipped": False, "rc": 1, "stdout": "", "stderr": "", "path": str(dist_exe)}

        if not dist_exe.exists():
            errors.append(f"dist app.exe not found: {dist_exe}")
        else:
            # If we have hashes for app.exe, enforce match before running
            expected_exe_sha = None
            if isinstance(file_sha256, dict):
                expected_exe_sha = file_sha256.get("app.exe")

            if expected_exe_sha:
                actual_exe_sha = sha256_file(dist_exe)
                if str(expected_exe_sha).lower() != actual_exe_sha.lower():
                    errors.append("dist exe sha256 mismatch vs release_hashes")
                else:
                    r, infra_flag = run_exe_help(dist_exe)
                    exec_res = r
                    if infra_flag:
                        infra = True
                        errors.append("exe check infra error")
                    elif int(exec_res.get("rc", 1)) != 0:
                        errors.append("exe --help failed")
            else:
                # No expected sha in hashes → still allow run, but result is meaningful only as smoke
                r, infra_flag = run_exe_help(dist_exe)
                exec_res = r
                if infra_flag:
                    infra = True
                    errors.append("exe check infra error")
                elif int(exec_res.get("rc", 1)) != 0:
                    errors.append("exe --help failed")

    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "release_id": args.release_id,
        "release_zip": str(release_zip),
        "zip_sha256": zip_sha,
        "release_hashes": str(release_hashes),
        "verify_mode": verify_mode,
        "expected_files": expected_files,
        "zip_files": zip_files,
        "errors": errors,
        "exec": exec_res,
    }

    if infra:
        return out, 2
    return out, (0 if len(errors) == 0 else 1)


def main() -> int:
    try:
        payload, code = main_inner()
        return emit_and_exit(payload, code)
    except Exception as e:  # noqa: BLE001
        code = classify_exit_code(e)
        payload = {"schema": SCHEMA, "error": {"kind": e.__class__.__name__, "message": str(e)}}
        return emit_and_exit(payload, code)


if __name__ == "__main__":
    raise SystemExit(main())
