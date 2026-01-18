from __future__ import annotations

import inspect
from pathlib import Path

from args.run.paper_loop_v0 import PaperLoopConfig, run_paper_loop

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"

CSV_IBKR = DATA_DIR / "hg_5m_bars_ibkr.csv"
CSV_SAMPLE = DATA_DIR / "hg_5m_bars_sample.csv"


def _choose_csv() -> Path:
    return CSV_IBKR if CSV_IBKR.exists() else CSV_SAMPLE


def _make_cfg(csv_path: Path) -> PaperLoopConfig:
    # Always pass tag/fresh_run, and try to pass csv_path under the correct param name
    kwargs = {"tag": "paper", "fresh_run": True}

    # Detect accepted constructor args
    names = set()
    try:
        sig = inspect.signature(PaperLoopConfig)
        names = set(sig.parameters.keys())
    except Exception:
        pass

    # Dataclass fallback
    if not names and hasattr(PaperLoopConfig, "__dataclass_fields__"):
        names = set(getattr(PaperLoopConfig, "__dataclass_fields__", {}).keys())

    for k in ("csv_path", "bars_csv_path", "input_csv_path", "input_csv", "bars_path"):
        if k in names:
            kwargs[k] = str(csv_path)
            break

    try:
        cfg = PaperLoopConfig(**kwargs)  # type: ignore[arg-type]
        return cfg
    except TypeError:
        # Fallback: create with minimal args, then best-effort set attribute
        cfg = PaperLoopConfig(tag="paper", fresh_run=True)  # type: ignore
        for attr in (
            "csv_path",
            "bars_csv_path",
            "input_csv_path",
            "input_csv",
            "bars_path",
        ):
            if hasattr(cfg, attr):
                try:
                    setattr(cfg, attr, str(csv_path))
                    break
                except Exception:
                    pass
        return cfg


def main() -> int:
    csv_path = _choose_csv()
    cfg = _make_cfg(csv_path)
    report = run_paper_loop(cfg)

    # Compact summary for terminal
    rid = report.get("run_id")
    out = report.get("outputs", {})
    h = report.get("harness_summary", {})
    w = report.get("wa_summary", {})

    print("PAPER_LOOP")
    print("csv_path:", str(csv_path))
    print("run_id:", rid)
    print("events_run:", out.get("events_run"))
    print("orders_paper:", out.get("orders_paper"))
    print("report_path:", report.get("report_path"))
    print("harness_processed:", h.get("processed"), "decisions:", h.get("ma_decisions"))
    print("wa_ticks:", w.get("ticks"), "orders_written:", w.get("orders_written"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
