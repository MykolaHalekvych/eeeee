
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from args.asys.as_v0_adapter import parse_bar_row, derive_ma_input_from_bar
from args.contracts.ma_input_contract import validate_ma_input
from args.contracts.paths_from_policy import inventory_from_policy_yaml
from args.ma.ma_runtime import eval_ma
from args.ma.policy_loader import load_policy
from args.run.live_safety_v0 import evaluate_live_safety


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
CONTROL_STATE_PATH = DATA_DIR / "control_state.json"


# -----------------------------
# Control plane helpers
# -----------------------------
def _load_control_state(path: Path = CONTROL_STATE_PATH) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        # BOM-safe for PowerShell UTF8
        obj = json.loads(path.read_text(encoding="utf-8-sig"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _normalize_global_mode(x: Any) -> str:
    s = str(x or "").strip().upper()
    if s in {"NO_TRADE", "ONLY_EXITS", "ALLOW_NEW_ENTRIES"}:
        return s
    return "NO_TRADE"


def _normalize_qc_default(x: Any) -> str:
    """
    Live safety expects PASS/FAIL. Treat OK as PASS for backward compatibility.
    Anything else => FAIL (conservative).
    """
    s = str(x or "").strip().upper()
    if s in {"PASS", "OK"}:
        return "PASS"
    if s == "FAIL":
        return "FAIL"
    return "FAIL"


# -----------------------------
# JSONL helpers
# -----------------------------
def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        f.write("\n")


def _load_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _bar_dict(bar) -> Dict[str, Any]:
    return {
        "ts": bar.ts,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


# -----------------------------
# Config
# -----------------------------
@dataclass(frozen=True)
class RunConfig:
    csv_path: Path
    policy_path: Path
    out_events: Path
    max_steps: int = 0  # 0 = all rows
    start_index: int = 0
    defaults: Optional[Dict[str, Any]] = None


# -----------------------------
# Harness
# -----------------------------
def run_harness(cfg: RunConfig) -> Dict[str, Any]:
    rows = _load_rows(cfg.csv_path)
    total_rows = len(rows)

    if cfg.start_index < 0 or cfg.start_index > total_rows:
        raise ValueError("start_index out of range")

    end_index = total_rows
    if cfg.max_steps and cfg.max_steps > 0:
        end_index = min(total_rows, cfg.start_index + cfg.max_steps)

    slice_rows = rows[cfg.start_index:end_index]

    policy = load_policy(str(cfg.policy_path))
    inv = inventory_from_policy_yaml(str(cfg.policy_path))

    defaults = cfg.defaults or {}

    # Control plane (Stage 5.8)
    control_state = _load_control_state()
    global_mode = _normalize_global_mode(control_state.get("global_mode"))

    # Defaults
    qc_default = _normalize_qc_default(defaults.get("qc", "PASS"))

    summary: Dict[str, Any] = {
        "total_rows": total_rows,
        "processed": 0,
        "contract_fail": 0,
        "safety_no_decision": 0,
        "halted": False,
        "halt_reason": "",
        "ma_decisions": {},
        "out_events": str(cfg.out_events),
        "control_state": {"global_mode": global_mode},
    }

    _append_jsonl(
        cfg.out_events,
        {
            "kind": "RUN_START",
            "csv": str(cfg.csv_path),
            "policy": str(cfg.policy_path),
            "policy_name": policy.name,
            "schema_version": policy.schema_version,
            "start_index": cfg.start_index,
            "end_index": end_index,
            "defaults": defaults,
            "control_state": {"global_mode": global_mode},
        },
    )

    for i, r in enumerate(slice_rows, start=cfg.start_index):
        bar = parse_bar_row(r)

        ma_input = derive_ma_input_from_bar(
            bar,
            qc=qc_default,
            stale_quotes=bool(defaults.get("stale_quotes", False)),
            missing_bars=int(defaults.get("missing_bars", 0)),
            timestamp_drift_ms=int(defaults.get("timestamp_drift_ms", 0)),
            kill_switch=bool(defaults.get("kill_switch", False)),
            liquidity=float(defaults.get("liquidity", 1.0)),
            margin_usage=float(defaults.get("margin_usage", 0.40)),
            confidence=float(defaults.get("confidence", 0.55)),
            regime=str(defaults.get("regime", "UNKNOWN")),
            tail_risk=str(defaults.get("tail_risk", "UNKNOWN")),
            correlation=float(defaults.get("correlation", 0.10)),
        )

        # Required base fields (MA runtime expects these)
        ma_input["instrument"] = str(defaults.get("instrument", "HG"))
        ma_input["timeframe"] = str(defaults.get("timeframe", "5m"))
        ma_input["env"] = str(defaults.get("env", "IBKR_PAPER_LABEL"))

        # Inject global_mode into existing exec.* namespace (avoid new top-level keys)
        if not isinstance(ma_input.get("exec"), dict):
            ma_input["exec"] = {}
        ma_input["exec"]["global_mode"] = global_mode

        # -----------------------------
        # Live safety gate (BEFORE MA, BEFORE WA)
        # -----------------------------
        safety = evaluate_live_safety(ma_input)

        if safety.decision == "HALT":
            summary["halted"] = True
            summary["halt_reason"] = safety.reason

            _append_jsonl(
                cfg.out_events,
                {
                    "kind": "SAFETY_HALT",
                    "index": i,
                    "ts": bar.ts,
                    "bar": _bar_dict(bar),
                    "safety": safety.to_dict(),
                    "ma_input": ma_input,
                },
            )
            break

        if safety.decision == "NO_DECISION":
            summary["processed"] += 1
            summary["safety_no_decision"] += 1
            summary["ma_decisions"]["NO_DECISION"] = summary["ma_decisions"].get("NO_DECISION", 0) + 1

            _append_jsonl(
                cfg.out_events,
                {
                    "kind": "SAFETY_NO_DECISION",
                    "index": i,
                    "ts": bar.ts,
                    "bar": _bar_dict(bar),
                    "safety": safety.to_dict(),
                    "ma_input": ma_input,
                },
            )

            # Emit a TICK record but keep it explicit:
            _append_jsonl(
                cfg.out_events,
                {
                    "kind": "TICK",
                    "index": i,
                    "ts": bar.ts,
                    "bar": _bar_dict(bar),
                    "ma_decision": "NO_DECISION",
                    "violations": [],
                    "risk_envelope": {
                        "mode": "NO_TRADE",
                        "enforced_no_trade": True,
                        "note": "live_safety_no_decision",
                    },
                    "position_state": ma_input.get("position_state") if isinstance(ma_input.get("position_state"), dict) else None,
                    "ma_input": ma_input,
                    "safety": safety.to_dict(),
                },
            )
            continue

        # -----------------------------
        # Contract check (policy-driven paths)
        # -----------------------------
        res = validate_ma_input(ma_input, inv.paths)
        if not res.ok:
            summary["contract_fail"] += 1
            _append_jsonl(
                cfg.out_events,
                {
                    "kind": "CONTRACT_FAIL",
                    "index": i,
                    "ts": bar.ts,
                    "bar": _bar_dict(bar),
                    "missing_paths": res.missing_paths,
                    "unknown_top_level_keys": res.unknown_top_level_keys,
                    "ma_input": ma_input,
                    "safety": safety.to_dict(),
                },
            )
            continue

        # -----------------------------
        # MA eval
        # -----------------------------
        report = eval_ma(policy, ma_input)
        decision = str(report.get("ma_decision", "UNKNOWN") or "UNKNOWN").upper()

        summary["processed"] += 1
        summary["ma_decisions"][decision] = summary["ma_decisions"].get(decision, 0) + 1

        risk_env = report.get("risk_envelope", {})
        if not isinstance(risk_env, dict):
            risk_env = {}

        _append_jsonl(
            cfg.out_events,
            {
                "kind": "TICK",
                "index": i,
                "ts": bar.ts,
                "bar": _bar_dict(bar),
                "ma_decision": decision,
                "violations": report.get("violations", []),
                "risk_envelope": risk_env,
                "position_state": ma_input.get("position_state") if isinstance(ma_input.get("position_state"), dict) else None,
                "ma_input": ma_input,
                "safety": safety.to_dict(),
            },
        )

    _append_jsonl(cfg.out_events, {"kind": "RUN_DONE", "summary": summary})
    return summary


def main() -> int:
    cfg_path = DATA_DIR / "run_config_v0.json"
    cfg_obj = json.loads(cfg_path.read_text(encoding="utf-8"))

    csv_path = REPO_ROOT / cfg_obj["csv_path"]
    policy_path = REPO_ROOT / cfg_obj["policy_path"]
    out_events = REPO_ROOT / cfg_obj["out_events"]
    max_steps = int(cfg_obj.get("max_steps", 0))
    start_index = int(cfg_obj.get("start_index", 0))
    defaults = cfg_obj.get("defaults", {})

    cfg = RunConfig(
        csv_path=csv_path,
        policy_path=policy_path,
        out_events=out_events,
        max_steps=max_steps,
        start_index=start_index,
        defaults=defaults,
    )

    summary = run_harness(cfg)
    print("RUN_SUMMARY:", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

