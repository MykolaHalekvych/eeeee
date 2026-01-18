from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.foundry.guard_v0 import find_repo_root, require_engine_repo
from args.foundry.manifests_v0 import ManifestError, load_factories, load_product
from args.foundry.gate_v0 import run_gate  # returns (exit_code, summary) with 0/1/2

EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2

# Deterministic ZIP metadata
FIXED_ZIP_DT = (1980, 1, 1, 0, 0, 0)
ZIP_MODE_644 = (0o644 & 0xFFFF) << 16


def dump(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def read_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    return json.loads(raw)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_rel(p: Path) -> str:
    # Always forward slashes for determinism
    return p.as_posix()


def expand_patterns(repo_root: Path, patterns: List[str]) -> List[Path]:
    """
    Expand glob-like patterns (including **) using deterministic matching.
    Patterns are repo-relative, e.g. "packs/x/**".
    """
    roots: List[Path] = []
    for pat in patterns:
        seg = pat.split("/")[0]
        if seg and seg not in ("**", "*"):
            roots.append(repo_root / seg)

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
                if "__pycache__" in rel or rel.endswith(".pyc"):
                    continue
                out.append(p)
                break

    uniq = sorted(
        {str(p.resolve()): p for p in out}.values(),
        key=lambda x: normalize_rel(x.relative_to(repo_root)),
    )
    return uniq


def policy_check_build(
    cp: Dict[str, Any], product_id: str, factory_id: str
) -> Tuple[bool, List[str]]:
    blocked: List[str] = []
    engine = cp.get("engine", {}) if isinstance(cp, dict) else {}
    perms = engine.get("permissions", {}) if isinstance(engine, dict) else {}
    allow = engine.get("allowlist", {}) if isinstance(engine, dict) else {}

    if not bool(perms.get("ALLOW_BUILD", False)):
        blocked.append("missing_permission:ALLOW_BUILD")

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


def copy_runbook_bytes(repo_root: Path, template_rel: str, out_path: Path) -> None:
    src = repo_root / template_rel
    if not src.exists():
        raise RuntimeError(f"runbook template missing: {template_rel}")
    out_path.write_bytes(src.read_bytes())


def make_zip_deterministic(zip_path: Path, repo_root: Path, files: List[Path]) -> None:
    """
    Deterministic zip:
      - fixed timestamps
      - fixed file mode
      - stable file order
      - ZIP_STORED (no zlib variability across machines)
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for p in files:
            rel = p.relative_to(repo_root)
            arc = "payload/" + normalize_rel(rel)
            data = p.read_bytes()

            zi = zipfile.ZipInfo(arc, date_time=FIXED_ZIP_DT)
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = ZIP_MODE_644

            zf.writestr(zi, data)


def write_evidence(
    out_path: Path,
    product_id: str,
    version: str,
    included: List[str],
    policy_summary: Dict[str, Any],
    gate_summary: Dict[str, Any],
) -> None:
    # Deterministic: no timestamps, no run_id
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
    lines.append(
        json.dumps(policy_summary, ensure_ascii=False, indent=2, sort_keys=True)
    )
    lines.append("```")
    lines.append("")
    lines.append("## Payload files included")
    lines.append(f"- count: {len(included)}")
    for r in included:
        lines.append(f"- {r}")
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
        dump(
            {
                "schema": "foundry_build_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": str(e),
            }
        )
        return EXIT_INFRA

    # Load control plane
    cp_path = (repo_root / Path(args.control_plane)).resolve()
    try:
        cp = read_json(cp_path)
    except Exception as e:
        dump(
            {
                "schema": "foundry_build_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": f"control_plane load failed: {type(e).__name__}: {e}",
            }
        )
        return EXIT_INFRA

    # Policy must allow build
    ok_policy, blocked_by = policy_check_build(cp, args.product, args.factory)
    if not ok_policy:
        dump(
            {
                "schema": "foundry_build_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "blocked_by": blocked_by,
            }
        )
        return EXIT_INFRA

    # Load manifests
    try:
        factories = load_factories(repo_root)
        if args.factory not in factories:
            raise ManifestError(f"unknown factory_id: {args.factory}")
        product = load_product(repo_root, args.product)
    except ManifestError as e:
        dump(
            {
                "schema": "foundry_build_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": str(e),
            }
        )
        return EXIT_INFRA

    # Run quality gate (M4) — must PASS before build
    gate_code, gate_summary = run_gate(repo_root)
    if gate_code != 0:
        dump(
            {
                "schema": "foundry_build_v0",
                "ok": False,
                "exit_code": gate_code,
                "error": "gate failed",
                "gate": gate_summary,
            }
        )
        return gate_code

    # Expand payload and validate build inputs
    try:
        files = expand_patterns(repo_root, product.include_paths)
        if len(files) == 0:
            raise RuntimeError("payload include_paths matched 0 files")
        rb_src = repo_root / product.runbook_template_path
        if not rb_src.exists():
            raise RuntimeError(
                f"missing runbook template: {product.runbook_template_path}"
            )
    except Exception as e:
        dump(
            {
                "schema": "foundry_build_v0",
                "ok": False,
                "exit_code": EXIT_EVAL_FAIL,
                "error": str(e),
            }
        )
        return EXIT_EVAL_FAIL

    included_rel = [normalize_rel(p.relative_to(repo_root)) for p in files]

    # Deterministic policy summary (no timestamps)
    policy_summary = {
        "schema": "engine_policy_summary_det_v0",
        "execution_mode": cp.get("execution_mode", "DRYRUN"),
        "permissions": (cp.get("engine", {}) or {}).get("permissions", {}),
        "allowlist": (cp.get("engine", {}) or {}).get("allowlist", {}),
        "control_plane_sha256": sha256_file(cp_path),
    }

    out_dir = repo_root / factories[args.factory].default_out_dir / product.product_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1) runbook.md
    runbook_path = out_dir / "runbook.md"
    try:
        copy_runbook_bytes(repo_root, product.runbook_template_path, runbook_path)
    except Exception as e:
        dump(
            {
                "schema": "foundry_build_v0",
                "ok": False,
                "exit_code": EXIT_EVAL_FAIL,
                "error": str(e),
            }
        )
        return EXIT_EVAL_FAIL

    # 2) bundle.zip (deterministic)
    bundle_path = out_dir / "bundle.zip"
    make_zip_deterministic(bundle_path, repo_root, files)

    # 3) evidence.md (deterministic)
    evidence_path = out_dir / "evidence.md"
    write_evidence(
        evidence_path,
        product.product_id,
        product.version,
        included_rel,
        policy_summary,
        gate_summary,
    )

    # 4) hashes.json (hash 3 files; no self-hash)
    hashes = {
        "schema": "engine_hashes_v0",
        "product_id": product.product_id,
        "version": product.version,
        "files": {
            "bundle.zip": sha256_file(bundle_path),
            "evidence.md": sha256_file(evidence_path),
            "runbook.md": sha256_file(runbook_path),
        },
    }
    hashes_path = out_dir / "hashes.json"
    hashes_path.write_text(
        json.dumps(hashes, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    dump(
        {
            "schema": "foundry_build_v0",
            "ok": True,
            "exit_code": 0,
            "out_dir": str(out_dir),
            "artifacts": {
                "bundle_zip": str(bundle_path),
                "evidence_md": str(evidence_path),
                "hashes_json": str(hashes_path),
                "runbook_md": str(runbook_path),
            },
            "payload_count": len(files),
        }
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
