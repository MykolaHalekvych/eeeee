from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from args.foundry.guard_v0 import find_repo_root, require_engine_repo
from args.foundry.manifests_v0 import load_factories, load_product, ManifestError

EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dump(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def load_control_plane(repo_root: Path, path: Path) -> Dict[str, Any]:
    raw = (repo_root / path).read_text(encoding="utf-8-sig")
    return json.loads(raw)


def allowlist_ok(cp: Dict[str, Any], product_id: str, factory_id: str) -> List[str]:
    blocked: List[str] = []
    engine = cp.get("engine", {}) if isinstance(cp, dict) else {}
    allow = engine.get("allowlist", {}) if isinstance(engine, dict) else {}
    products = allow.get("products", [])
    factories = allow.get("factories", [])

    # safe-by-default: пустой allowlist = ничего нельзя
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
    return blocked


def main() -> int:
    ap = argparse.ArgumentParser(prog="foundry_plan_v0")
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
                "schema": "foundry_plan_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": str(e),
            }
        )
        return EXIT_INFRA

    try:
        cp = load_control_plane(repo_root, Path(args.control_plane))
    except Exception as e:
        dump(
            {
                "schema": "foundry_plan_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": f"control_plane load failed: {type(e).__name__}: {e}",
            }
        )
        return EXIT_INFRA

    blocked = allowlist_ok(cp, args.product, args.factory)
    if blocked:
        dump(
            {
                "schema": "foundry_plan_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "product": args.product,
                "factory": args.factory,
                "blocked_by": blocked,
            }
        )
        return EXIT_INFRA

    try:
        factories = load_factories(repo_root)
        if args.factory not in factories:
            raise ManifestError(f"unknown factory_id: {args.factory}")
        product = load_product(repo_root, args.product)
    except ManifestError as e:
        dump(
            {
                "schema": "foundry_plan_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": str(e),
            }
        )
        return EXIT_INFRA

    # План шагов (заранее под M5/M6):
    steps = [
        {
            "step_id": "S0_POLICY_SUMMARY",
            "action": "policy_summary",
            "required_permission": None,
        },
        {"step_id": "S1_GATE", "action": "gate", "required_permission": None},
        {
            "step_id": "S2_BUILD_BUNDLE",
            "action": "build_bundle",
            "required_permission": "ALLOW_BUILD",
        },
        {
            "step_id": "S3_HASHES",
            "action": "write_hashes",
            "required_permission": "ALLOW_BUILD",
        },
        {
            "step_id": "S4_EVIDENCE",
            "action": "write_evidence",
            "required_permission": "ALLOW_BUILD",
        },
        {
            "step_id": "S5_RUNBOOK",
            "action": "write_runbook",
            "required_permission": "ALLOW_BUILD",
        },
        {
            "step_id": "S6_RELEASE",
            "action": "release",
            "required_permission": "ALLOW_EXPORT",
        },
    ]

    dump(
        {
            "schema": "foundry_plan_v0",
            "ok": True,
            "exit_code": 0,
            "ts_utc": utc_now(),
            "product": {
                "product_id": product.product_id,
                "title": product.title,
                "version": product.version,
                "payload_include_paths": product.include_paths,
                "runbook_template_path": product.runbook_template_path,
            },
            "factory": {
                "factory_id": args.factory,
                "kind": factories[args.factory].kind,
                "default_out_dir": factories[args.factory].default_out_dir,
            },
            "steps": steps,
        }
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
