
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from args.foundry.guard_v0 import find_repo_root, require_engine_repo

EXIT_OK = 0
EXIT_INFRA = 2


def dump(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def read_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    return json.loads(raw)


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def required_perm(action: str) -> Optional[str]:
    a = action.lower().strip()
    if a == "build":
        return "ALLOW_BUILD"
    if a in ("export", "release"):
        return "ALLOW_EXPORT"
    if a == "apply":
        return "ALLOW_APPLY"
    if a in ("delete", "clean"):
        return "ALLOW_DELETE"
    return None


def build_policy_summary_det(repo_root: Path, cp_path: Path, cp: Dict[str, Any]) -> Dict[str, Any]:
    engine = cp.get("engine", {}) if isinstance(cp, dict) else {}
    perms = engine.get("permissions", {}) if isinstance(engine, dict) else {}
    allow = engine.get("allowlist", {}) if isinstance(engine, dict) else {}

    return {
        "schema": "foundry_policy_summary_det_v0",
        "repo_root": str(repo_root),
        "control_plane_path": str(cp_path),
        "execution_mode": cp.get("execution_mode", "DRYRUN"),
        "permissions": {
            "ALLOW_BUILD": bool(perms.get("ALLOW_BUILD", False)),
            "ALLOW_EXPORT": bool(perms.get("ALLOW_EXPORT", False)),
            "ALLOW_APPLY": bool(perms.get("ALLOW_APPLY", False)),
            "ALLOW_DELETE": bool(perms.get("ALLOW_DELETE", False)),
        },
        "allowlist": {
            "products": allow.get("products", []),
            "factories": allow.get("factories", []),
        },
    }


def check_action(
    summary: Dict[str, Any],
    action: str,
    product: Optional[str],
    factory: Optional[str],
) -> Tuple[bool, List[str]]:
    blocked: List[str] = []

    perm = required_perm(action)
    if perm is not None:
        perms = summary.get("permissions", {})
        if not bool(perms.get(perm, False)):
            blocked.append(f"missing_permission:{perm}")

    # safe-by-default allowlist
    allow = summary.get("allowlist", {})
    products = allow.get("products", [])
    factories = allow.get("factories", [])

    if product is not None:
        if not isinstance(products, list) or len(products) == 0 or product not in products:
            blocked.append("product_not_in_allowlist")

    if factory is not None:
        if not isinstance(factories, list) or len(factories) == 0 or factory not in factories:
            blocked.append("factory_not_in_allowlist")

    return (len(blocked) == 0), blocked


def main() -> int:
    ap = argparse.ArgumentParser(prog="args.foundry.policy_v0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_emit = sub.add_parser("policy", help="emit deterministic policy summary")
    p_emit.add_argument("--control-plane", default="control_plane.json")
    p_emit.add_argument("--out", default=None, help="optional output path; if omitted, only stdout")

    p_check = sub.add_parser("check", help="check if action is allowed under current policy")
    p_check.add_argument("--control-plane", default="control_plane.json")
    p_check.add_argument("--action", required=True)
    p_check.add_argument("--product", default=None)
    p_check.add_argument("--factory", default=None)
    p_check.add_argument("--write-summary", action="store_true", help="write args/data/policy_summary_latest.json")

    args = ap.parse_args()

    repo_root = find_repo_root(Path("."))
    try:
        require_engine_repo(repo_root)
    except Exception as e:
        dump({"schema": "foundry_policy_v0", "ok": False, "exit_code": EXIT_INFRA, "error": str(e)})
        return EXIT_INFRA

    cp_path = (repo_root / Path(args.control_plane)).resolve()
    try:
        cp = read_json(cp_path)
    except Exception as e:
        dump(
            {
                "schema": "foundry_policy_v0",
                "ok": False,
                "exit_code": EXIT_INFRA,
                "error": f"control_plane load failed: {type(e).__name__}: {e}",
            }
        )
        return EXIT_INFRA

    summary = build_policy_summary_det(repo_root, cp_path, cp)

    if args.cmd == "policy":
        if args.out:
            out_path = (repo_root / Path(args.out)).resolve()
            write_json(out_path, summary)
            dump({"schema": "foundry_policy_emit_v0", "ok": True, "exit_code": 0, "out": str(out_path)})
        else:
            dump({"schema": "foundry_policy_emit_v0", "ok": True, "exit_code": 0, "summary": summary})
        return EXIT_OK

    # check
    if args.write_summary:
        # runtime artifact; .gitignore should exclude it
        write_json((repo_root / Path("args/data/policy_summary_latest.json")).resolve(), summary)

    ok, blocked_by = check_action(summary, args.action, args.product, args.factory)

    dump(
        {
            "schema": "foundry_policy_check_v0",
            "ok": ok,
            "exit_code": (EXIT_OK if ok else EXIT_INFRA),
            "action": args.action,
            "product": args.product,
            "factory": args.factory,
            "blocked_by": blocked_by,
        }
    )
    return EXIT_OK if ok else EXIT_INFRA


if __name__ == "__main__":
    raise SystemExit(main())
