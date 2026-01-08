
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
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


def _find_member_by_basename(z: zipfile.ZipFile, wanted_base_lower: str) -> str | None:
    # Return first member whose basename matches (case-insensitive), ignoring directories.
    for n in z.namelist():
        if not n or n.endswith("/"):
            continue
        if Path(n).name.lower() == wanted_base_lower:
            return n
    return None


def _extract_member_atomic(z: zipfile.ZipFile, member: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink(missing_ok=True)

    with z.open(member, "r") as src, tmp_path.open("wb") as dst:
        shutil.copyfileobj(src, dst)

    # Replace atomically-ish: remove old then rename
    if out_path.exists():
        out_path.unlink(missing_ok=True)
    tmp_path.replace(out_path)


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

    # Verify content + hashes without extraction
    zip_files: List[str] = []
    duplicate_basenames: List[str] = []
    zip_members_by_base: Dict[str, str] = {}

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
                        errors.append(f"zip read/hash error for {name}: {e.__class__.__name__}")
                        infra = True

    except zipfile.BadZipFile:
        errors.append("zip bad file")
        zip_files = []
    except Exception as e:  # noqa: BLE001
        errors.append(f"zip open error: {e.__class__.__name__}")
        zip_files = []
        infra = True

    # EXE check (opt-in only) — runs from release zip member, not from dist/
    exec_res: Dict[str, Any] = {"skipped": True, "rc": 0, "stdout": "", "stderr": ""}
    if args.run_exe_check == "YES":
        exec_res = {"skipped": False, "rc": 1, "stdout": "", "stderr": "", "path": ""}

        try:
            with zipfile.ZipFile(str(release_zip), "r") as z:
                member = _find_member_by_basename(z, "app.exe")
                if member is None:
                    errors.append("missing app.exe in zip")
                    exec_res = {"skipped": False, "rc": 1, "stdout": "", "stderr": "", "path": "", "source": "release_zip"}
                else:
                    # Stable repo temp path (short, deterministic)
                    exe_tmp_root = repo / "args" / "data" / "tmp" / "release_verify_v0_exe"
                    exe_tmp_dir = exe_tmp_root / hashlib.sha256(args.release_id.encode("utf-8")).hexdigest()[:12]
                    exe_out = exe_tmp_dir / "app.exe"

                    _extract_member_atomic(z, member, exe_out)

                    # Verify exe sha if available
                    expected_exe_sha = file_sha256.get("app.exe") if isinstance(file_sha256, dict) else None
                    if expected_exe_sha:
                        actual_exe_sha = sha256_file(exe_out)
                        if str(expected_exe_sha).lower() != actual_exe_sha.lower():
                            errors.append("exe sha256 mismatch vs release_hashes")
                            exec_res = {
                                "skipped": False,
                                "rc": 1,
                                "stdout": "",
                                "stderr": "",
                                "path": str(exe_out),
                                "source": "release_zip",
                                "zip_member": member,
                            }
                        else:
                            r, infra_flag = run_exe_help(exe_out)
                            r["source"] = "release_zip"
                            r["zip_member"] = member
                            exec_res = r
                            if infra_flag:
                                infra = True
                                errors.append("exe check infra error")
                            elif int(exec_res.get("rc", 1)) != 0:
                                errors.append("exe --help failed")
                    else:
                        r, infra_flag = run_exe_help(exe_out)
                        r["source"] = "release_zip"
                        r["zip_member"] = member
                        exec_res = r
                        if infra_flag:
                            infra = True
                            errors.append("exe check infra error")
                        elif int(exec_res.get("rc", 1)) != 0:
                            errors.append("exe --help failed")

        except zipfile.BadZipFile:
            errors.append("zip bad file")
        except Exception as e:  # noqa: BLE001
            errors.append(f"exe check error: {e.__class__.__name__}")
            infra = True

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
