from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.foundry.guard_v0 import find_repo_root, require_engine_repo
from args.foundry.manifests_v0 import load_factories, load_product, ManifestError

EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2

FIXED_ZIP_DT = (1980, 1, 1, 0, 0, 0)

def dump(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")

def read_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    return json.loads(raw)

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def normalize_rel(p: Path) -> str:
    # always forward slashes for determinism
    return p.as_posix()

def expand_patterns(repo_root: Path, patterns: List[str]) -> List[Path]:
    """
    Expands glob-like patterns with **, but using deterministic matching.
    Patterns are relative to repo_root, like "packs/x/**".
    """
    # Pre-list all candidate files under repo_root for deterministic fnmatch
    # To keep it cheap, we restrict scan to top-level roots inferred from patterns.
    roots: List[Path] = []
    for pat in patterns:
        # take first segment before wildcard as root
        seg = pat.split("/")[0]
        if seg and seg not in ("**", "*"):
            roots.append(repo_root / seg)
    # if nothing inferred, fallback to repo_root
    scan_roots = sorted(set([r for r in roots if r.exists()])) or [repo_root]

    candidates: List[Path] = []
    for r in scan_roots:
        for p in r.rglob("*"):
            if p.is_file():
                candidates.append(p)

    out: List[Path] = []
    for p in candidates:
        rel = normalize_rel(p.relative_to(repo_root))
        for pat in patterns:
            if fnmatch.fnmatch(rel, pat):
                # ignore pycache/pyc deterministically
                if "__pycache__" in rel or rel.endswith(".pyc"):
                    continue
                out.append(p)
                break

    # unique + sorted
    uniq = sorted({str(p.resolve()): p for p in out}.values(), key=lambda x: normalize_rel(x.relative_to(repo_root)))
    return uniq

def policy_check_build(cp: Dict[str, Any], product_id: str, factory_id: str) -> Tuple[bool, List[str]]:
    blocked: List[str] = []
    engine = cp.get("engine", {}) if isinstance(cp, dict) else {}
    perms = engine.get("permissions", {}) if isinstance(engine, dict) else {}
    allow = engine.get("allowlist", {}) if isinstance(engine, dict) else {}

    if not bool(perms.get("ALLOW_BUILD", False)):
        blocked.append("missing_permission:ALLOW_BUILD")

    products = allow.get("products", [])
    factories = allow.get("factories", [])
    if not isinstance(products, list) or len(products) == 0 or product_id not in products:
        blocked.append("product_not_in_allowlist")
    if not isinstance(factories, list) or len(factories) == 0 or factory_id not in factories:
        blocked.append("factory_not_in_allowlist")

    return (len(blocked) == 0), blocked

def write_runbook(repo_root: Path, template_rel: str, out_path: Path) -> None:
    src = repo_root / template_rel
    if not src.exists():
        raise RuntimeError(f"runbook template missing: {template_rel}")
    out_path.write_text(src.read_text(encoding="utf-8-sig"), encoding="utf-8")

def make_zip_deterministic(zip_path: Path, repo_root: Path, files: List[Path]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            rel = p.relative_to(repo_root)
            arc = "payload/" + normalize_rel(rel)
            data = p.read_bytes()

            zi = zipfile.ZipInfo(arc, date_time=FIXED_ZIP_DT)
            zi.compress_type = zipfile.ZIP_DEFLATED
            # normalize permissions (644)
            zi.external_attr = (0o644 & 0xFFFF) << 16

            zf.writestr(zi, data)

def write_evidence(out_path: Path, product_id: str, version: str, included: List[str], policy_summary: Dict[str, Any], gate_summary: Dict[str, Any]) -> None:
    # Deterministic: no timestamps, no run_id.
    lines: List[str] = []
    lines.append(f"# Evidence — {product_id} v{version}")
    lines.append("")
    lines.append("## Gate summary")
    lines.append("```json")
    lines.append(json.dumps(gate_summary, ensure_ascii=False, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Policy summary (deterministic)")
    lines.append("```json")
    lines.append(json.dumps(policy_summary, ensure_ascii=False, indent=2, sort_keys=True))
    lines.append("```")
    lines.append("")
    lines.append("## Payload files included")
    lines.append(f"- count: {len(included)}")
    for rel in included:
        lines.append(f"- {rel}")
    lines.append("")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def main() -> int:
    ap = argparse.ArgumentParser(prog="foundry_build_v0")
    ap.add_argument("--control-plane", default="control_plane.json")
    ap.add_argument("--product", required=True)
    ap.add_argument("--factory", default="local")
    args = ap.parse_args()

    repo_root = find_repo_root(Path("."))
    try:
        require_engine_repo(repo_root)
    except Exception as e:
        dump({"schema":"foundry_build_v0","ok":False,"exit_code":EXIT_INFRA,"error":str(e)})
        return EXIT_INFRA

    # load control plane
    try:
        cp = read_json((repo_root / Path(args.control_plane)).resolve())
    except Exception as e:
        dump({"schema":"foundry_build_v0","ok":False,"exit_code":EXIT_INFRA,"error":f"control_plane load failed: {type(e).__name__}: {e}"})
        return EXIT_INFRA

    # policy must allow build
    ok_policy, blocked_by = policy_check_build(cp, args.product, args.factory)
    if not ok_policy:
        dump({"schema":"foundry_build_v0","ok":False,"exit_code":EXIT_INFRA,"blocked_by":blocked_by})
        return EXIT_INFRA

    # load manifests
    try:
        factories = load_factories(repo_root)
        if args.factory not in factories:
            raise ManifestError(f"unknown factory_id: {args.factory}")
        product = load_product(repo_root, args.product)
    except ManifestError as e:
        dump({"schema":"foundry_build_v0","ok":False,"exit_code":EXIT_INFRA,"error":str(e)})
        return EXIT_INFRA

    out_dir = repo_root / factories[args.factory].default_out_dir / product.product_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Gate v0 (minimal): make sure payload expands to non-empty and runbook template exists
    try:
        files = expand_patterns(repo_root, product.include_paths)
        if len(files) == 0:
            raise RuntimeError("payload include_paths matched 0 files")
        rb_src = repo_root / product.runbook_template_path
        if not rb_src.exists():
            raise RuntimeError(f"missing runbook template: {product.runbook_template_path}")
    except Exception as e:
        dump({"schema":"foundry_build_v0","ok":False,"exit_code":EXIT_EVAL_FAIL,"error":str(e)})
        return EXIT_EVAL_FAIL

    included_rel = [normalize_rel(p.relative_to(repo_root)) for p in files]

    # Deterministic policy summary (no timestamps)
    policy_summary = {
        "schema": "engine_policy_summary_det_v0",
        "execution_mode": cp.get("execution_mode", "DRYRUN"),
        "permissions": (cp.get("engine", {}) or {}).get("permissions", {}),
        "allowlist": (cp.get("engine", {}) or {}).get("allowlist", {}),
        "control_plane_sha256": sha256_file((repo_root / Path(args.control_plane)).resolve()),
    }

    gate_summary = {
        "schema": "engine_gate_summary_v0",
        "ok": True,
        "checks": [
            {"id":"payload_nonempty", "ok": True, "count": len(files)},
            {"id":"runbook_template_exists", "ok": True, "path": product.runbook_template_path},
        ]
    }

    # 1) runbook.md
    runbook_path = out_dir / "runbook.md"
    try:
        write_runbook(repo_root, product.runbook_template_path, runbook_path)
    except Exception as e:
        dump({"schema":"foundry_build_v0","ok":False,"exit_code":EXIT_EVAL_FAIL,"error":str(e)})
        return EXIT_EVAL_FAIL

    # 2) bundle.zip (deterministic)
    bundle_path = out_dir / "bundle.zip"
    make_zip_deterministic(bundle_path, repo_root, files)

    # 3) evidence.md (deterministic)
    evidence_path = out_dir / "evidence.md"
    write_evidence(evidence_path, product.product_id, product.version, included_rel, policy_summary, gate_summary)

    # 4) hashes.json (hash 3 files; no self-hash)
    hashes = {
        "schema": "engine_hashes_v0",
        "product_id": product.product_id,
        "version": product.version,
        "files": {
            "bundle.zip": sha256_file(bundle_path),
            "evidence.md": sha256_file(evidence_path),
            "runbook.md": sha256_file(runbook_path),
        }
    }
    hashes_path = out_dir / "hashes.json"
    hashes_path.write_text(json.dumps(hashes, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    dump({
        "schema":"foundry_build_v0",
        "ok": True,
        "exit_code": 0,
        "out_dir": str(out_dir),
        "artifacts": {
            "bundle_zip": str(bundle_path),
            "evidence_md": str(evidence_path),
            "hashes_json": str(hashes_path),
            "runbook_md": str(runbook_path),
        },
        "payload_count": len(files)
    })
    return EXIT_OK

if __name__ == "__main__":
    raise SystemExit(main())
