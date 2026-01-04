from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from args.foundry.guard_v0 import find_repo_root, require_engine_repo

EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def load_json(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig")  # BOM-safe
    return json.loads(raw)

def dump_stdout(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

def required_perm(action: str) -> str | None:
    a = action.lower().strip()
    if a == "build": return "ALLOW_BUILD"
    if a in ("export", "release"): return "ALLOW_EXPORT"
    if a == "apply": return "ALLOW_APPLY"
    if a in ("delete", "clean"): return "ALLOW_DELETE"
    return None

def main() -> int:
    ap = argparse.ArgumentParser(prog="args.foundry.policy_v0")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_policy = sub.add_parser("policy")
    p_policy.add_argument("--control-plane", default="control_plane.json")
    p_policy.add_argument("--out", default=r"args\data\policy_summary_latest.json")

    p_check = sub.add_parser("check")
    p_check.add_argument("--control-plane", default="control_plane.json")
    p_check.add_argument("--action", required=True)
    p_check.add_argument("--product", default=None)
    p_check.add_argument("--factory", default=None)
    p_check.add_argument("--write-summary", action="store_true")

    args = ap.parse_args()

    repo_root = find_repo_root(Path("."))
    try:
        require_engine_repo(repo_root)
    except Exception as e:
        dump_stdout({"schema":"foundry_policy_v0","ok":False,"exit_code":EXIT_INFRA,"error":str(e)})
        return EXIT_INFRA

    cp_path = (repo_root / Path(args.control_plane)).resolve()
    try:
        cp = load_json(cp_path)
    except Exception as e:
        dump_stdout({"schema":"foundry_policy_v0","ok":False,"exit_code":EXIT_INFRA,"error":f"control_plane load failed: {type(e).__name__}: {e}"})
        return EXIT_INFRA

    engine = cp.get("engine", {}) if isinstance(cp, dict) else {}
    perms = engine.get("permissions", {}) if isinstance(engine, dict) else {}
    allow = engine.get("allowlist", {}) if isinstance(engine, dict) else {}

    summary = {
        "schema": "foundry_policy_summary_v0",
        "ts_utc": utc_now(),
        "repo_root": str(repo_root),
        "control_plane_path": str(cp_path),
        "execution_mode": cp.get("execution_mode", "DRYRUN"),
        "permissions": {
            "ALLOW_BUILD": bool(perms.get("ALLOW_BUILD", False)),
            "ALLOW_EXPORT": bool(perms.get("ALLOW_EXPORT", False)),
            "ALLOW_APPLY": bool(perms.get("ALLOW_APPLY", False)),
            "ALLOW_DELETE": bool(perms.get("ALLOW_DELETE", False))
        },
        "allowlist": {
            "products": allow.get("products", []),
            "factories": allow.get("factories", [])
        }
    }

    if args.cmd == "policy":
        out_path = (repo_root / Path(args.out)).resolve()
        write_json(out_path, summary)
        dump_stdout({"schema":"foundry_policy_emit_v0","ok":True,"exit_code":0,"out":str(out_path)})
        return EXIT_OK

    blocked_by = []
    perm = required_perm(args.action)
    if perm and not summary["permissions"].get(perm, False):
        blocked_by.append(f"missing_permission:{perm}")

    # safe-by-default allowlist
    products = summary["allowlist"]["products"]
    factories = summary["allowlist"]["factories"]
    if args.product is not None:
        if not isinstance(products, list) or len(products) == 0 or args.product not in products:
            blocked_by.append("product_not_in_allowlist")
    if args.factory is not None:
        if not isinstance(factories, list) or len(factories) == 0 or args.factory not in factories:
            blocked_by.append("factory_not_in_allowlist")

    if args.write_summary:
        write_json((repo_root / r"args\data\policy_summary_latest.json").resolve(), summary)

    ok = len(blocked_by) == 0
    dump_stdout({
        "schema":"foundry_policy_check_v0",
        "ok": ok,
        "exit_code": (0 if ok else EXIT_INFRA),
        "action": args.action,
        "product": args.product,
        "factory": args.factory,
        "blocked_by": blocked_by
    })
    return (0 if ok else EXIT_INFRA)

if __name__ == "__main__":
    raise SystemExit(main())
