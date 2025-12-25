from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from args.run.csv_meta_v1 import attach_csv_meta
from args.run.run_harness_v0 import RunConfig, run_harness


@dataclass(frozen=True)
class PaperLoopConfig:
    tag: str = "paper"
    fresh_run: bool = True
    # Optional override for csv_path:
    # - can be absolute: C:\...\hg_5m_bars_ibkr.csv
    # - or repo-relative: args\data\hg_5m_bars_ibkr.csv
    csv_path: str | None = None


def _now_id() -> str:
    return uuid.uuid4().hex[:10]


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _copy_run_config_template(repo_root: Path) -> Dict[str, Any]:
    cfg_path = repo_root / "args" / "data" / "run_config_v0.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _resolve_path(repo_root: Path, p: Optional[str]) -> Optional[Path]:
    if not p:
        return None
    pp = Path(str(p))
    return pp if pp.is_absolute() else (repo_root / pp)


def _choose_csv_path(repo_root: Path, rc: Dict[str, Any], cfg: PaperLoopConfig) -> Path:
    """
    Selection rule:
    1) If cfg.csv_path is set -> use it (absolute or repo-relative). If missing on disk, fallback.
    2) Else use rc["csv_path"].
    3) If chosen == sample AND hg_5m_bars_ibkr.csv exists -> switch to IBKR csv.
    4) If chosen missing -> prefer IBKR if exists, else sample if exists, else keep chosen.
    """
    data_dir = repo_root / "args" / "data"
    csv_ibkr = data_dir / "hg_5m_bars_ibkr.csv"
    csv_sample = data_dir / "hg_5m_bars_sample.csv"

    chosen = _resolve_path(repo_root, cfg.csv_path) or _resolve_path(repo_root, str(rc.get("csv_path") or ""))

    # If still None -> pick best available
    if chosen is None:
        return csv_ibkr if csv_ibkr.exists() else csv_sample

    # Prefer IBKR only when config points to sample (so we don't override intentional custom paths)
    try:
        if csv_ibkr.exists() and chosen.resolve() == csv_sample.resolve():
            chosen = csv_ibkr
    except Exception:
        # If resolve fails, still prefer IBKR when it exists and chosen looks like sample
        if csv_ibkr.exists() and str(chosen).lower().endswith("hg_5m_bars_sample.csv"):
            chosen = csv_ibkr

    # If chosen doesn't exist -> fallback to best available
    if not chosen.exists():
        if csv_ibkr.exists():
            return csv_ibkr
        if csv_sample.exists():
            return csv_sample

    return chosen


def run_paper_loop(cfg: PaperLoopConfig) -> Dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    run_id = _now_id()

    # Load run_config_v0.json (template)
    rc = _copy_run_config_template(repo_root)

    # Build per-run event/output paths (fresh files, no accumulation)
    out_events = repo_root / "args" / "data" / f"events_run_{run_id}.jsonl"
    out_orders = repo_root / "args" / "data" / f"orders_paper_{run_id}.jsonl"

    # Resolve inputs
    csv_path = _choose_csv_path(repo_root, rc, cfg)
    policy_path = _resolve_path(repo_root, str(rc.get("policy_path") or "")) or (repo_root / "args" / "data" / "invariants_hg_v0.yaml")

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

    # 3) Report (write into logs)
    logs_dir = repo_root / "args" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    report_path = logs_dir / f"run_report_{run_id}_{cfg.tag}.json"

    report: Dict[str, Any] = {
        "run_id": run_id,
        "tag": cfg.tag,
        "fresh_run": cfg.fresh_run,
        "report_path": str(report_path),
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

    # Attach CSV meta (if <csv>.meta.json exists)
    attach_csv_meta(report, csv_path)

    # Persist report
    _write_json(report_path, report)

    return report


def main() -> int:
    cfg = PaperLoopConfig(tag="paper", fresh_run=True)
    report = run_paper_loop(cfg)

    # Compact summary
    out = report.get("outputs", {})
    h = report.get("harness_summary", {})
    w = report.get("wa_summary", {})

    print("PAPER_LOOP")
    print("csv_path:", report.get("inputs", {}).get("csv_path"))
    print("run_id:", report.get("run_id"))
    print("events_run:", out.get("events_run"))
    print("orders_paper:", out.get("orders_paper"))
    print("report_path:", report.get("report_path"))
    print("harness_processed:", h.get("processed"), "decisions:", h.get("ma_decisions"))
    print("wa_ticks:", w.get("ticks"), "orders_written:", w.get("orders_written"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
