from __future__ import annotations

import argparse
import time
from pathlib import Path

from .execution_v1 import Engine, OrderSpec
from .ibkr_file_adapter_v0 import IbkrFileAdapterV0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--positions", default=r"args\data\ibkr_positions_live.json")
    ap.add_argument("--orders", default=r"args\data\ibkr_open_orders_live.json")
    ap.add_argument("--events", default=r"args\data\ibkr_events_live.jsonl")
    ap.add_argument("--cursor", default=r"args\data\ibkr_events.cursor.json")
    ap.add_argument("--reset-cursor", action="store_true")

    ap.add_argument("--order-id", type=int, default=1018)
    ap.add_argument("--symbol", default="AAPL")
    ap.add_argument("--side", default="SELL")
    ap.add_argument("--qty", type=float, default=2.0)

    ap.add_argument("--steps", type=int, default=160)
    ap.add_argument("--sleep", type=float, default=0.25)
    args = ap.parse_args()

    repo = Path(args.repo)
    cursor = repo / args.cursor
    if args.reset_cursor:
        try:
            cursor.unlink()
        except FileNotFoundError:
            pass

    adapter = IbkrFileAdapterV0(
        repo_root=repo,
        positions_path=repo / args.positions,
        open_orders_path=repo / args.orders,
        events_jsonl_path=repo / args.events,
        events_cursor_path=cursor,
    )
    adapter.connect()

    cp_path = repo / "args" / "data" / "control_plane.json"
    if not cp_path.exists():
        cp_path = repo / "control_plane.json"

    run_id = f"ibkr_lifecycle_{int(time.time())}"
    eng = Engine(
        repo_root=repo, run_id=run_id, control_plane_path=cp_path, broker=adapter
    )

    intent_id = f"adopt_{args.order_id}"
    eng.adopt_order(
        intent_id=intent_id,
        order_id=int(args.order_id),
        order=OrderSpec(symbol=args.symbol, side=args.side, qty=float(args.qty)),
        client_order_id=f"oid_{args.order_id}",
        remaining_qty=float(args.qty),
    )

    print({"snapshot0": adapter.snapshot()})

    last = None
    for i in range(int(args.steps)):
        eng.step()
        t = eng.state.tickets[intent_id]
        cur = (
            t.state.value,
            t.terminal.value if t.terminal else None,
            round(t.filled_qty, 6),
            round(t.remaining_qty, 6),
        )
        if cur != last:
            print(
                {
                    "i": i,
                    "state": cur[0],
                    "terminal": cur[1],
                    "filled": cur[2],
                    "remaining": cur[3],
                    "events_seen": eng.state.counters.get("events_seen"),
                }
            )
            last = cur
        if t.is_terminal():
            break
        time.sleep(float(args.sleep))

    t = eng.state.tickets[intent_id]
    print(
        {
            "run_dir": str(eng.run_dir),
            "ticket_state": t.state.value,
            "terminal": t.terminal.value if t.terminal else None,
            "terminal_reason": t.terminal_reason,
            "reconcile_last_ratio": eng.state.reconcile_last_ratio,
            "events_seen": eng.state.counters.get("events_seen"),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
