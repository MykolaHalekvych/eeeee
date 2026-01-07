from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "workspace_apply_patch_v0"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_rel(p: str) -> Path:
    if not isinstance(p, str) or not p:
        raise ValueError("path must be non-empty string")
    if "\\" in p:
        raise ValueError("backslashes not allowed in patch paths")
    if p.startswith("/") or p.startswith("~") or ":" in p:
        raise ValueError("absolute paths not allowed")
    parts = p.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError("path traversal not allowed")
    return Path(*parts)


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="Create workspace from template and apply codegen patch JSON")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--product-id", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--codegen", required=True, help="runs/<run_id>/codegen_output.json")
    ap.add_argument("--workspaces-dir", default=None)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()

    # Load job_request for allowlist enforcement
    runs_dir = repo / "args" / "data" / "runs" / args.run_id
    jr_path = runs_dir / "job_request.json"
    allowed_paths: list[str] | None = None
    if jr_path.exists():
        jr = json.loads(jr_path.read_text(encoding="utf-8-sig"))
        apaths = jr.get("allowed_paths")
        if apaths is not None:
            if not isinstance(apaths, list) or not all(isinstance(x, str) for x in apaths):
                raise ValueError("job_request.allowed_paths must be an array of strings")
            allowed_paths = apaths

    workspaces_dir = Path(args.workspaces_dir).resolve() if args.workspaces_dir else (repo / "args" / "data" / "workspaces")
    ws_root = workspaces_dir / args.run_id / "workspace"

    manifest_path = repo / "manifests" / "products" / f"{args.product_id}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"product manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    template_dir = repo / str(manifest["template_dir"])

    if ws_root.exists():
        shutil.rmtree(ws_root)
    ws_root.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(template_dir, ws_root)

    codegen_path = Path(args.codegen).resolve()
    patch = json.loads(codegen_path.read_text(encoding="utf-8-sig"))
    if patch.get("schema") != "codegen_patch_v0":
        raise ValueError("codegen_output.json must be schema=codegen_patch_v0")

    ops = patch.get("ops", [])
    if not isinstance(ops, list) or not ops:
        raise ValueError("patch.ops must be non-empty")

    written: list[str] = []
    blocked: list[str] = []

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"ops[{i}] must be object")
        if op.get("op") != "write_file":
            raise ValueError(f"ops[{i}].op must be write_file")

        rel = safe_rel(op.get("path"))
        rel_s = str(rel).replace("\\", "/")

        if allowed_paths is not None and rel_s not in allowed_paths:
            blocked.append(rel_s)
            continue

        full = (ws_root / rel).resolve()
        full.relative_to(ws_root)

        content = op.get("content", "")
        if not isinstance(content, str):
            raise ValueError(f"ops[{i}].content must be string")

        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="\n")
        written.append(rel_s)

    if blocked:
        raise ValueError(f"blocked writes outside allowed_paths: {blocked}")

    report = {
        "schema": SCHEMA,
        "ok": True,
        "exit_code": 0,
        "ts_utc": utc_ts(),
        "run_id": args.run_id,
        "product_id": args.product_id,
        "workspace": str(ws_root),
        "codegen_sha256": sha256_file(codegen_path),
        "files_written": written,
        "allowed_paths": allowed_paths,
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
