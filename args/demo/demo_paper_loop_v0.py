from __future__ import annotations

import json
from args.run.paper_loop_v0 import PaperLoopConfig, run_paper_loop


def main() -> int:
    cfg = PaperLoopConfig(tag="paper", fresh_run=True)
    report = run_paper_loop(cfg)

    # Compact summary for terminal
    rid = report.get("run_id")
    out = report.get("outputs", {})
    h = report.get("harness_summary", {})
    w = report.get("wa_summary", {})

    print("PAPER_LOOP")
    print("run_id:", rid)
    print("events_run:", out.get("events_run"))
    print("orders_paper:", out.get("orders_paper"))
    print("report_path:", report.get("report_path"))
    print("harness_processed:", h.get("processed"), "decisions:", h.get("ma_decisions"))
    print("wa_ticks:", w.get("ticks"), "orders_written:", w.get("orders_written"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
