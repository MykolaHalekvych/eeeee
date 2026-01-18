from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.foundry.guard_v0 import find_repo_root, require_engine_repo
from args.foundry.manifests_v0 import ManifestError, load_factories, load_product

EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2

FIXED_ZIP_DT = (1980, 1, 1, 0, 0, 0)
ZIP_MODE_644 = (0o644 & 0xFFFF) << 16

REQUIRED_DIST_FILES = ["bundle.zip", "evidence.md", "hashes.json", "runbook.md"]


def dump(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def read_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    return json.loads(raw)


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_short_head(repo_root: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            s = (r.stdout or "").strip()
            return s if s else "nogit"
        return "nogit"
    except Exception:
        return "nogit"


def policy_check_export(
    cp: Dict[str, Any], product_id: str, factory_id: str
) -> Tuple[bool, List[str]]:
    blocked: List[str] = []
    engine = cp.get("engine", {}) if isinstance(cp, dict) else {}
    perms = engine.get("permissions", {}) if isinstance(engine, dict) else {}
    allow = engine.get("allowlist", {}) if isinstance(engine, dict) else {}

    if not bool(perms.get("ALLOW_EXPORT", False)):
        blocked.append("missing_permission:ALLOW_EXPORT")

    products = allow.get("products", [])
    factories = allow.get("factories", [])
    if (
        not isinstance(products, list)
        or len(products) == 0
        or product_id not in products
    ):
        blocked.append("product_not_in_allowlist")
    if (
        not isinstance(factories, list)
        or len(factories) == 0
        or factory_id not in factories
    ):
        blocked.append("factory_not_in_allowlist")

    return (len(blocked) == 0), blocked


def validate_dist(out_dir: Path) -> Tuple[bool, str]:
    for name in REQUIRED_DIST_FILES:
        if not (out_dir / name).exists():
            return False, f"missing dist file: {name}"
    return True, ""


def validate_hashes(out_dir: Path) -> Tuple[bool, str, Dict[str, Any]]:
    hashes_path = out_dir / "hashes.json"
    try:
        obj = read_json(hashes_path)
    except Exception as e:
        return False, f"hashes.json load failed: {type(e).__name__}: {e}", {}

    if obj.get("schema") != "engine_hashes_v0":
        return False, "hashes.json schema must be engine_hashes_v0", obj

    files = obj.get("files")
    if not isinstance(files, dict):
        return False, "hashes.json.files must be dict", obj

    # Must include these keys (bundle/evidence/runbook)
    for k in ("bundle.zip", "evidence.md", "runbook.md"):
        if k not in files:
            return False, f"hashes.json.files missing key: {k}", obj

    # Recompute and compare
    for k in ("bundle.zip", "evidence.md", "runbook.md"):
        p = out_dir / k
        got = sha256_file(p)
        exp = str(files.get(k))
        if got != exp:
            return False, f"hash mismatch for {k}: expected={exp} got={got}", obj

    return True, "", obj


def make_release_zip(zip_path: Path, product_id: str, out_dir: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    # deterministic: ZIP_STORED + fixed metadata + stable order
    with zipfile.ZipFile(zip_path, "w") as zf:
        for name in REQUIRED_DIST_FILES:
            p = out_dir / name
            data = p.read_bytes()
            arc = f"{product_id}/{name}"
            zi = zipfile.ZipInfo(arc, date_time=FIXED_ZIP_DT)
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = ZIP_MODE_644
            zf.writestr(zi, data)


def main() -> int:
    ap = argparse.ArgumentParser(prog="foundry_release_v0")
    ap.add_argument("--control-plane", default="control_plane.json")
    ap.add_argument("--product", required=True)
    ap.add_argument("--factory", default="local")
    ap.add_argument("--release-id", default=None)
    args = ap.parse_args()

    repo_root = find_repo_root(Path("."))
    try:
        require_engine_repo(repo_root)
    except Exception as e:
        dump(
            {
                "schema": "foundry_release_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": str(e),
            }
        )
        return EXIT_INFRA

    # control plane
    cp_path = (repo_root / Path(args.control_plane)).resolve()
    try:
        cp = read_json(cp_path)
    except Exception as e:
        dump(
            {
                "schema": "foundry_release_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": f"control_plane load failed: {type(e).__name__}: {e}",
            }
        )
        return EXIT_INFRA

    ok_pol, blocked_by = policy_check_export(cp, args.product, args.factory)
    if not ok_pol:
        dump(
            {
                "schema": "foundry_release_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "blocked_by": blocked_by,
            }
        )
        return EXIT_INFRA

    # manifests
    try:
        factories = load_factories(repo_root)
        if args.factory not in factories:
            raise ManifestError(f"unknown factory_id: {args.factory}")
        product = load_product(repo_root, args.product)
    except ManifestError as e:
        dump(
            {
                "schema": "foundry_release_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": str(e),
            }
        )
        return EXIT_INFRA

    out_dir = repo_root / factories[args.factory].default_out_dir / product.product_id

    ok_dist, dist_err = validate_dist(out_dir)
    if not ok_dist:
        dump(
            {
                "schema": "foundry_release_v0",
                "ok": False,
                "exit_code": EXIT_EVAL_FAIL,
                "error": dist_err,
            }
        )
        return EXIT_EVAL_FAIL

    ok_hash, hash_err, hashes_obj = validate_hashes(out_dir)
    if not ok_hash:
        dump(
            {
                "schema": "foundry_release_v0",
                "ok": False,
                "exit_code": EXIT_EVAL_FAIL,
                "error": hash_err,
            }
        )
        return EXIT_EVAL_FAIL

    git_short = git_short_head(repo_root)
    release_id = (
        args.release_id or f"{product.product_id}__v{product.version}__{git_short}"
    )

    releases_dir = repo_root / "dist" / "releases"
    zip_path = releases_dir / f"{release_id}.zip"

    try:
        make_release_zip(zip_path, product.product_id, out_dir)
    except Exception as e:
        dump(
            {
                "schema": "foundry_release_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": f"zip write failed: {type(e).__name__}: {e}",
            }
        )
        return EXIT_INFRA

    release_hashes = {
        "schema": "engine_release_hashes_v0",
        "release_id": release_id,
        "product_id": product.product_id,
        "version": product.version,
        "git_short": git_short,
        "files": {
            f"{release_id}.zip": sha256_file(zip_path),
        },
        "inputs": {
            "dist_dir": str(out_dir),
            "hashes_json": sha256_file(out_dir / "hashes.json"),
            "control_plane_sha256": sha256_file(cp_path),
        },
    }
    release_hash_path = releases_dir / f"{release_id}.hashes.json"
    try:
        write_json(release_hash_path, release_hashes)
    except Exception as e:
        dump(
            {
                "schema": "foundry_release_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": f"release hashes write failed: {type(e).__name__}: {e}",
            }
        )
        return EXIT_INFRA

    dump(
        {
            "schema": "foundry_release_v0",
            "ok": True,
            "exit_code": 0,
            "product_id": product.product_id,
            "version": product.version,
            "release_id": release_id,
            "artifacts": {
                "release_zip": str(zip_path),
                "release_hashes": str(release_hash_path),
            },
        }
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
