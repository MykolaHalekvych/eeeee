from __future__ import annotations

from typing import Any, Dict, List, Tuple


SCHEMA = "product_manifest_v1"
VERSION = 1


def validate_manifest(obj: Any) -> Tuple[bool, List[str]]:
    errs: List[str] = []
    if not isinstance(obj, dict):
        return False, ["manifest must be an object"]

    if obj.get("schema") != SCHEMA:
        errs.append("schema must be product_manifest_v1")
    if obj.get("version") != VERSION:
        errs.append("version must be 1")

    pid = obj.get("product_id")
    if not isinstance(pid, str) or not pid.strip():
        errs.append("product_id must be a non-empty string")

    cmds = obj.get("commands")
    if not isinstance(cmds, list) or not cmds:
        errs.append("commands must be a non-empty list")
    else:
        for i, c in enumerate(cmds):
            if not isinstance(c, dict):
                errs.append(f"commands[{i}] must be an object")
                continue
            name = c.get("name")
            if not isinstance(name, str) or not name.strip():
                errs.append(f"commands[{i}].name must be a non-empty string")

    return (len(errs) == 0), errs


def minimal_manifest(product_id: str) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "product_id": product_id,
        "commands": [
            {"name": "ping"},
            {"name": "version"},
            {"name": "hash"},
            {"name": "selftest"},
            {"name": "manifest-validate"},
        ],
    }
