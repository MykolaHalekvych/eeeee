from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Set

import yaml


@dataclass(frozen=True)
class PathInventory:
    paths: List[str]
    ops: List[str]
    blocks: List[str]


def _walk(obj: Any, paths: Set[str], ops: Set[str]) -> None:
    """
    Recursively find dicts shaped like:
      { path: "...", op: "...", value: ... }
    """
    if isinstance(obj, dict):
        # detect atom
        if "path" in obj and "op" in obj:
            p = obj.get("path")
            o = obj.get("op")
            if isinstance(p, str) and p.strip():
                paths.add(p.strip())
            if isinstance(o, str) and o.strip():
                ops.add(o.strip())
        # continue walk
        for v in obj.values():
            _walk(v, paths, ops)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, paths, ops)


def inventory_from_policy_yaml(policy_path: str) -> PathInventory:
    p = Path(policy_path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Policy YAML top-level must be dict")

    blocks_obj = raw.get("blocks", {})
    blocks: List[str] = []
    if isinstance(blocks_obj, dict):
        blocks = sorted(list(blocks_obj.keys()))

    paths: Set[str] = set()
    ops: Set[str] = set()
    _walk(raw, paths, ops)

    return PathInventory(
        paths=sorted(list(paths)), ops=sorted(list(ops)), blocks=blocks
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    policy = repo_root / "args" / "data" / "invariants_hg_v0.yaml"
    inv = inventory_from_policy_yaml(str(policy))

    print("POLICY:", policy)
    print("BLOCKS:", ", ".join(inv.blocks))
    print("OPS:", ", ".join(inv.ops))
    print("PATHS:")
    for p in inv.paths:
        print(" -", p)
    print(f"TOTAL_PATHS={len(inv.paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
