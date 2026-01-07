from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

SCHEMA = "release_pack_v0"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_product_manifest(repo: Path, product_id: str) -> Dict[str, Any]:
    p = repo / "manifests" / "products" / f"{product_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"product manifest not found: {p}")
    data = read_json(p)
    if data.get("product_id") != product_id:
        raise ValueError("product_id mismatch in manifest")
    return data


def deterministic_write(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    zi = zipfile.ZipInfo(filename=arcname)
    zi.date_time = (1980, 1, 1, 0, 0, 0)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(zi, data, compress_type=zipfile.ZIP_DEFLATED)


def emit_and_exit(payload: Dict[str, Any], code: int) -> int:
    payload["schema"] = payload.get("schema", SCHEMA)
    payload["ts_utc"] = payload.get("ts_utc", utc_ts())
    payload["exit_code"] = int(code)
    payload["ok"] = (int(code) == 0)
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write(s)
    sys.stdout.flush()
    return int(code)


def classify_exit_code(exc: Exception) -> int:
    # FAIL: missing inputs / validation / content issues
    if isinstance(exc, (FileNotFoundError, ValueError)):
        return 1
    # INFRA: OS/IO/locks/etc.
    return 2


def _tmp_path_for(dst: Path) -> Path:
    # same dir => atomic replace possible on Windows
    return dst.with_name(dst.name + f".tmp.{uuid.uuid4().hex}")


def _atomic_write(dst: Path, writer: Callable[[Path], None]) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(dst)
    try:
        if tmp.exists():
            tmp.unlink()
        writer(tmp)
        # atomic swap on same filesystem
        os.replace(str(tmp), str(dst))
    finally:
        # best-effort cleanup if something failed before replace
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def main_inner() -> Tuple[Dict[str, Any], int]:
    ap = argparse.ArgumentParser(description="Package EXE Pack v0 into dist/releases/<release_id>.zip")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--product-id", required=True)
    ap.add_argument("--product-dist", default=None, help="Override dist/<product_id> directory")
    ap.add_argument("--releases-dir", default=None, help="Override dist/releases directory")
    ap.add_argument("--release-id", default=None, help="Optional explicit release_id")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    manifest = load_product_manifest(repo, args.product_id)
    version = str(manifest.get("version", "0.0.0"))

    product_dist = Path(args.product_dist).resolve() if args.product_dist else (repo / "dist" / args.product_id)
    releases_dir = Path(args.releases_dir).resolve() if args.releases_dir else (repo / "dist" / "releases")
    releases_dir.mkdir(parents=True, exist_ok=True)

    required: List[str] = [
        "app.exe",
        "config.example.json",
        "runbook.md",
        "evidence.md",
        "hashes.json",
    ]
    optional: List[str] = [
        "acceptance_gate.json",
        "acceptance_gate.stdout.txt",
        "acceptance_gate.stderr.txt",
    ]

    missing = [name for name in required if not (product_dist / name).exists()]
    if missing:
        raise FileNotFoundError(f"missing required files in {product_dist}: {missing}")

    included: List[str] = list(required)
    present_optional: List[str] = []
    for name in optional:
        if (product_dist / name).exists():
            included.append(name)
            present_optional.append(name)

    hashes_path = product_dist / "hashes.json"
    hashes_sha = sha256_file(hashes_path)

    if not present_optional:
        tag = hashes_sha[:10]
        tag_mode = "legacy_hashes_prefix"
    else:
        parts = [hashes_sha] + [sha256_file(product_dist / n) for n in present_optional]
        tag = sha256_bytes(("|".join(parts)).encode("utf-8"))[:10]
        tag_mode = "hashes_plus_optional"

    release_id = args.release_id or f"{args.product_id}__v{version}__{tag}"
    release_zip = releases_dir / f"{release_id}.zip"
    release_hashes = releases_dir / f"{release_id}.hashes.json"

    # Atomic write: zip (tmp -> replace)
    def _write_zip(tmp_zip: Path) -> None:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in included:
                data = (product_dist / name).read_bytes()
                deterministic_write(zf, name, data)

    _atomic_write(release_zip, _write_zip)

    zip_sha = sha256_file(release_zip)

    file_sha256: Dict[str, str] = {name: sha256_file(product_dist / name) for name in included}

    outer = {
        "schema": "release_hashes_v0",
        "ts_utc": utc_ts(),
        "product_id": args.product_id,
        "version": version,
        "release_id": release_id,
        "tag_mode": tag_mode,
        "included_files": included,
        "file_sha256": file_sha256,
        "artifacts": {
            "release_zip": {"path": str(release_zip), "sha256": zip_sha},
            "hashes_json": {"path": str(hashes_path), "sha256": hashes_sha},
        },
    }
    outer_text = json.dumps(outer, indent=2, ensure_ascii=False) + "\n"

    # Atomic write: hashes json (tmp -> replace)
    def _write_hashes(tmp_json: Path) -> None:
        with tmp_json.open("w", encoding="utf-8", newline="\n") as f:
            f.write(outer_text)

    _atomic_write(release_hashes, _write_hashes)

    out = {
        "schema": SCHEMA,
        "product_id": args.product_id,
        "version": version,
        "release_id": release_id,
        "product_dist": str(product_dist),
        "release_zip": str(release_zip),
        "release_hashes": str(release_hashes),
        "files": included,
        "optional_included": present_optional,
        "tag_mode": tag_mode,
        "sha256": {"zip": zip_sha, "hashes_json": hashes_sha},
    }
    return out, 0


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
