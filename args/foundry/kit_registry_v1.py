from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------
# Exit codes (project standard)
# ----------------------------
RC_OK = 0
RC_FAIL = 1  # user / validation failure
RC_INFRA = 2  # file / json / unexpected infra failure


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def read_json_file(path: Path) -> Any:
    # utf-8-sig is intentional: Windows BOM-safe
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise
    return json.loads(raw)


def load_registry(path: Path) -> Dict[str, Any]:
    obj = read_json_file(path)
    if not isinstance(obj, dict) or obj.get("schema") != "kits_registry_v1":
        raise ValueError(
            "invalid kits registry schema (expected schema=kits_registry_v1)"
        )
    kits = obj.get("kits")
    if not isinstance(kits, list):
        raise ValueError("kits must be a list")
    return obj


def list_kits(reg: Dict[str, Any]) -> List[Dict[str, Any]]:
    kits_any = reg.get("kits", [])
    out: List[Dict[str, Any]] = []
    for k in kits_any:
        if isinstance(k, dict):
            out.append(k)
    return out


def find_kit(reg: Dict[str, Any], kit_id: str) -> Dict[str, Any]:
    for k in list_kits(reg):
        if k.get("kit_id") == kit_id:
            return k
    raise KeyError(f"kit_id not found: {kit_id}")


def _kit_summary_for_list(k: Dict[str, Any]) -> str:
    # Prefer explicit summary fields if present; otherwise try requirements_default.summary
    for key in ("summary", "description", "title"):
        v = k.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    rd = k.get("requirements_default")
    if isinstance(rd, dict):
        v = rd.get("summary")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _kit_default_product_id(k: Dict[str, Any]) -> str:
    v = k.get("default_product_id")
    return str(v).strip() if v is not None else ""


def _print_kits_table(kits: List[Dict[str, Any]]) -> None:
    rows: List[Tuple[str, str, str]] = []
    for k in kits:
        kit_id = str(k.get("kit_id") or "").strip()
        if not kit_id:
            continue
        summary = _kit_summary_for_list(k)
        default_pid = _kit_default_product_id(k)
        rows.append((kit_id, default_pid, summary))

    rows.sort(key=lambda r: r[0])

    w1 = max([len("kit_id")] + [len(r[0]) for r in rows]) if rows else len("kit_id")
    w2 = (
        max([len("default_product_id")] + [len(r[1]) for r in rows])
        if rows
        else len("default_product_id")
    )

    print(f"{'kit_id'.ljust(w1)}  {'default_product_id'.ljust(w2)}  summary")
    print(f"{'-' * w1}  {'-' * w2}  " + "-" * 40)
    for kit_id, default_pid, summary in rows:
        print(f"{kit_id.ljust(w1)}  {default_pid.ljust(w2)}  {summary}")
    print(f"\nTotal kits: {len(rows)}")


def build_job_request_v1(
    kit: Dict[str, Any], product_id: str, summary: str
) -> Dict[str, Any]:
    workspace = kit.get("workspace", {})
    allowed_paths = kit.get("allowed_paths", [])
    kit_id = kit.get("kit_id")

    if not isinstance(kit_id, str) or not kit_id.strip():
        raise ValueError("kit_id missing/invalid in kit")
    kit_id = kit_id.strip()

    if not isinstance(workspace, dict):
        workspace = {}
    if not isinstance(allowed_paths, list):
        allowed_paths = []

    entrypoint = workspace.get("entrypoint", "src/main.py")
    cli_entry = workspace.get("cli_entry", "src/cli.py")
    stdlib_only = bool(workspace.get("stdlib_only", True))

    if not isinstance(entrypoint, str) or not entrypoint.strip():
        entrypoint = "src/main.py"
    if not isinstance(cli_entry, str) or not cli_entry.strip():
        cli_entry = "src/cli.py"

    # Keep contract stable: job_request_v1 + version + requirements.summary + allowed_paths
    return {
        "schema": "job_request_v1",
        "version": 1,
        "product_id": product_id,
        "kit_id": kit_id,
        "workspace": {
            "entrypoint": entrypoint,
            "cli_entry": cli_entry,
            "stdlib_only": stdlib_only,
        },
        "requirements": {"summary": summary},
        "allowed_paths": allowed_paths,
    }


def resolve_defaults(
    kit: Dict[str, Any], product_id_arg: Optional[str], summary_arg: Optional[str]
) -> Tuple[str, str]:
    # product_id: arg overrides kit.default_product_id
    product_id = (product_id_arg or "").strip()
    if not product_id:
        product_id = _kit_default_product_id(kit)

    # summary: arg overrides kit.requirements_default.summary (fallback: kit summary fields)
    summary = (summary_arg or "").strip()
    if not summary:
        rd = kit.get("requirements_default")
        if isinstance(rd, dict):
            v = rd.get("summary")
            if isinstance(v, str):
                summary = v.strip()
    if not summary:
        summary = _kit_summary_for_list(kit).strip()

    return product_id, summary


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--kits", default="manifests/kits_v1.json", help="Path to kits registry JSON"
    )
    ap.add_argument("--list", action="store_true", help="List available kits and exit")
    ap.add_argument(
        "--json", action="store_true", help="With --list: output JSON instead of table"
    )
    ap.add_argument(
        "--kit-id", required=False, help="Kit id to generate job_request_v1"
    )
    ap.add_argument(
        "--product-id",
        required=False,
        help="Override product_id (otherwise default_product_id from kit)",
    )
    ap.add_argument(
        "--summary",
        required=False,
        help="Override summary (otherwise requirements_default.summary from kit)",
    )

    args = ap.parse_args(argv)

    kits_path = Path(args.kits)

    try:
        reg = load_registry(kits_path)
    except FileNotFoundError:
        eprint(f"INFRA: kits registry not found: {kits_path}")
        return RC_INFRA
    except json.JSONDecodeError as ex:
        eprint(f"INFRA: kits registry JSON parse failed: {kits_path} ({ex})")
        return RC_INFRA
    except Exception as ex:
        eprint(f"INFRA: kits registry load failed: {kits_path} ({ex})")
        return RC_INFRA

    if args.list:
        kits = list_kits(reg)
        if args.json:
            # stdout is JSON only
            print(json.dumps(kits, ensure_ascii=False, indent=2))
        else:
            _print_kits_table(kits)
        return RC_OK

    # Generate job_request
    kit_id = (args.kit_id or "").strip()
    if not kit_id:
        eprint("FAIL: --kit-id is required (or use --list)")
        return RC_FAIL

    try:
        kit = find_kit(reg, kit_id)
    except KeyError as ex:
        eprint(f"FAIL: {ex}")
        return RC_FAIL
    except Exception as ex:
        eprint(f"INFRA: unexpected error while finding kit: {ex}")
        return RC_INFRA

    product_id, summary = resolve_defaults(kit, args.product_id, args.summary)

    if not product_id:
        eprint(
            "FAIL: product_id missing (provide --product-id or set default_product_id in kit)"
        )
        return RC_FAIL
    if not summary:
        eprint(
            "FAIL: summary missing (provide --summary or set requirements_default.summary in kit)"
        )
        return RC_FAIL

    try:
        jr = build_job_request_v1(kit, product_id=product_id, summary=summary)
    except Exception as ex:
        eprint(f"INFRA: failed to build job_request_v1: {ex}")
        return RC_INFRA

    # stdout must be JSON only
    print(json.dumps(jr, ensure_ascii=False, indent=2))
    return RC_OK


if __name__ == "__main__":
    raise SystemExit(main())
