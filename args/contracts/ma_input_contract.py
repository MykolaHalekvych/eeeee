from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.contracts.paths_from_policy import inventory_from_policy_yaml


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    missing_paths: List[str]
    present_paths: List[str]
    unknown_top_level_keys: List[str]


def _get_by_dotpath(obj: Dict[str, Any], path: str) -> Tuple[bool, Any]:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return False, None
        if part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def validate_ma_input(input_obj: Dict[str, Any], allowed_paths: List[str]) -> ContractResult:
    missing: List[str] = []
    present: List[str] = []

    for p in allowed_paths:
        ok, _ = _get_by_dotpath(input_obj, p)
        if ok:
            present.append(p)
        else:
            missing.append(p)

    # sanity: ensure top-level keys are reasonable (not strict)
    allowed_top = sorted({p.split(".")[0] for p in allowed_paths})
    unknown_top = []
    for k in input_obj.keys():
        if k not in allowed_top:
            unknown_top.append(str(k))

    return ContractResult(
        ok=(len(missing) == 0),
        missing_paths=missing,
        present_paths=present,
        unknown_top_level_keys=sorted(unknown_top),
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    policy = repo_root / "args" / "data" / "invariants_hg_v0.yaml"
    inv = inventory_from_policy_yaml(str(policy))

    # Minimal sample input skeleton (no values substituted)
    sample = {
        "data": {
            "missing_bars": 0,
            "qc": "OK",
            "stale_quotes": False,
            "timestamp_drift_ms": 0,
        },
        "exec": {"kill_switch": False},
        "market": {"liquidity": 1.0},
        "risk": {"margin_usage": 0.0},
        "state": {"confidence": 1.0, "regime": "UNKNOWN", "tail_risk": "UNKNOWN"},
        "stats": {"correlation": 0.0},
    }

    res = validate_ma_input(sample, inv.paths)
    print("OK:", res.ok)
    print("MISSING_PATHS:", len(res.missing_paths))
    for p in res.missing_paths:
        print(" -", p)
    print("UNKNOWN_TOP_LEVEL_KEYS:", ", ".join(res.unknown_top_level_keys) if res.unknown_top_level_keys else "(none)")
    return 0 if res.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
