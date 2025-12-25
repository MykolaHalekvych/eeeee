from __future__ import annotations
from args.run.csv_meta_v1 import attach_csv_meta


import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

from args.run.run_harness_v0 import RunConfig, run_harness
from args.demo.demo_wa_v0_stub import main as wa_stub_main


@dataclass(frozen=True)
class PaperLoopConfig:
    tag: str = "paper"
    fresh_run: bool = True


def _now_id() -> str:
    # short run id
    return uuid.uuid4().hex[:10]


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _copy_run_config_template(repo_root: Path) -> Dict[str, Any]:
    cfg_path = repo_root / "args" / "data" / "run_config_v0.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def run_paper_loop(cfg: PaperLoopConfig) -> Dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    run_id = _now_id()

    # Load run_config_v0.json (template)
    rc = _copy_run_config_template(repo_root)

    # Build per-run event/output paths (fresh files, no accumulation)
    out_events = repo_root / "args" / "data" / f"events_run_{run_id}.jsonl"
    out_orders = repo_root / "args" / "data" / f"orders_paper_{run_id}.jsonl"

    # Reuse existing paths from config
    csv_path = repo_root / rc["csv_path"]
    policy_path = repo_root / rc["policy_path"]
    max_steps = int(rc.get("max_steps", 0))
    start_index = int(rc.get("start_index", 0))
    defaults = rc.get("defaults", {})

    # Fresh-run: ensure output files start empty
    if cfg.fresh_run:
        if out_events.exists():
            out_events.unlink()
        if out_orders.exists():
            out_orders.unlink()

    # 1) Run harness (writes events_run_<run_id>.jsonl)
    hcfg = RunConfig(
        csv_path=csv_path,
        policy_path=policy_path,
        out_events=out_events,
        max_steps=max_steps,
        start_index=start_index,
        defaults=defaults,
    )
    h_summary = run_harness(hcfg)

    # 2) Run WA stub against events_in -> write orders_out
    # We reuse existing demo logic by temporarily pointing its expected filenames.
    # Keep it simple: just run minimal inline conversion here to avoid side effects.
    from args.wa.wa_v0_stub import decide_wa_action  # local import to avoid cycles

    def iter_jsonl(path: Path):
        if not path.exists():
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        yield obj
                except json.JSONDecodeError:
                    continue

    def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False))
            f.write("\n")

    n_ticks = 0
    n_orders = 0
    for ev in iter_jsonl(out_events):
        if ev.get("kind") != "TICK":
            continue
        n_ticks += 1
        ma_decision = ev.get("ma_decision")
        violations = ev.get("violations", [])
        if not isinstance(violations, list):
            violations = []
        action = decide_wa_action(ma_decision, violations)
        order = {
            "kind": "ORDER_PAPER",
            "source": "WA_V0",
            "run_id": run_id,
            "index": ev.get("index"),
            "ts": ev.get("ts"),
            "instrument": defaults.get("instrument", "HG"),
            "timeframe": defaults.get("timeframe", "5m"),
            "ma_decision": (ma_decision or "UNKNOWN"),
            "wa_action": action.to_dict(),
        }
        append_jsonl(out_orders, order)
        n_orders += 1

    # 3) Report
    report = {
        "run_id": run_id,
        "tag": cfg.tag,
        "fresh_run": cfg.fresh_run,
        "inputs": {
            "csv_path": str(csv_path),
            "policy_path": str(policy_path),
        },
        "outputs": {
            "events_run": str(out_events),
            "orders_paper": str(out_orders),
        },
        "harness_summary": h_summary,
        "wa_summary": {"ticks": n_ticks, "orders_written": n_orders},
    }

    # Save report into logs
    logs_dir = repo_root / "args" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    report_path = logs_dir / f"run_report_{run_id}_{cfg.tag}.json"
    _write_json(report_path, report)
    report["report_path"] = str(report_path)

    return report


def main() -> int:
    cfg = PaperLoopConfig(tag="paper", fresh_run=True)
    report = run_paper_loop(cfg)
    print("PAPER_LOOP_REPORT:", json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
