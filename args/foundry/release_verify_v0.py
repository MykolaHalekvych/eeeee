
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
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
    if isinstance(exc, (FileNotFoundError, ValueError)):
        return 1
    return 2


def run_exe_help(exe_path: Path) -> Dict[str, Any]:
    p = subprocess.run([str(exe_path), "--help"], capture_output=True, text=True, check=False)
    return {"rc": int(p.returncode), "stdout": p.stdout or "", "stderr": p.stderr or ""}


def main_inner() -> Tuple[Dict[str, Any], int]:
    ap = argparse.ArgumentParser(description="Verify release zip against release_hashes_v0")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--release-id", required=True)
    ap.add_argument("--releases-dir", default=None)
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

    zip_sha = sha256_file(release_zip)
    expected_zip_sha = (((rh.get("artifacts") or {}).get("release_zip") or {}).get("sha256"))
    if expected_zip_sha and str(expected_zip_sha).lower() != zip_sha.lower():
        errors.append("zip sha256 mismatch")

    tmp_root = Path(tempfile.mkdtemp(prefix="release_verify_v0__"))
    extracted = tmp_root / "release"
    extracted.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(release_zip), "r") as z:
        z.extractall(str(extracted))

    zip_files = sorted([p.name for p in extracted.iterdir() if p.is_file()])

    included_files = rh.get("included_files")
    file_sha256 = rh.get("file_sha256")

    verify_mode = "legacy_5_files"
    expected_files: List[str] = ["app.exe", "config.example.json", "evidence.md", "hashes.json", "runbook.md"]

    if isinstance(included_files, list) and isinstance(file_sha256, dict):
        verify_mode = "manifest_superset"
        expected_files = sorted([str(x) for x in included_files])

        expected_set = set(expected_files)
        got_set = set(zip_files)

        if expected_set != got_set:
            errors.append(f"zip content mismatch: expected {sorted(expected_set)} got {sorted(got_set)}")

        for name in expected_files:
            expected_sha = file_sha256.get(name)
            if not expected_sha:
                errors.append(f"missing expected sha256 for {name} in release_hashes")
                continue
            p = extracted / name
            if not p.exists():
                errors.append(f"missing file in zip: {name}")
                continue
            actual = sha256_file(p)
            if str(expected_sha).lower() != actual.lower():
                errors.append(f"sha256 mismatch for {name}")
    else:
        if zip_files != expected_files:
            errors.append(f"zip content mismatch: expected {expected_files} got {zip_files}")

    exe_path = extracted / "app.exe"
    exec_res = {"rc": 1, "stdout": "", "stderr": ""}
    if exe_path.exists():
        exec_res = run_exe_help(exe_path)
        if exec_res["rc"] != 0:
            errors.append("exe --help failed")
    else:
        errors.append("missing app.exe in zip")

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
