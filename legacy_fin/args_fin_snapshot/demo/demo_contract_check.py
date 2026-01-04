from __future__ import annotations

import json
from pathlib import Path

from args.contracts.paths_from_policy import inventory_from_policy_yaml
from args.contracts.ma_input_contract import validate_ma_input


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    policy = repo_root / "args" / "data" / "invariants_hg_v0.yaml"
    sample = repo_root / "args" / "data" / "ma_input_sample.json"

    inv = inventory_from_policy_yaml(str(policy))
    obj = json.loads(sample.read_text(encoding="utf-8"))

    res = validate_ma_input(obj, inv.paths)

    print("CONTRACT_CHECK:", sample)
    print("OK:", res.ok)
    print("MISSING_PATHS:", len(res.missing_paths))
    if res.missing_paths:
        for p in res.missing_paths:
            print(" -", p)
    print("UNKNOWN_TOP_LEVEL_KEYS:", ", ".join(res.unknown_top_level_keys) if res.unknown_top_level_keys else "(none)")

    return 0 if res.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
