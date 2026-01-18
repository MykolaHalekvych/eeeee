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

    # cursor controls
    ap.add_argument("--cursor", default=r"args\data\ibkr_events.cursor.json")
    ap.add_argument("--reset-cursor", action="store_true")

    ap.add_argument("--run-id", default=None)

    # optional adopt
    ap.add_argument("--adopt-order-id", type=int, default=1018)
    ap.add_argument("--adopt-symbol", default="AAPL")
    ap.add_argument("--adopt-side", default="SELL")
    ap.add_argument("--adopt-qty", type=float, default=2.0)

    ap.add_argument("--steps", type=int, default=25)
    args = ap.parse_args()

    repo = Path(args.repo)
    run_id = args.run_id or f"ibkr_bridge_{int(time.time())}"

    positions_path = repo / args.positions
    orders_path = repo / args.orders
    events_path = repo / args.events
    cursor_path = repo / args.cursor

    if args.reset_cursor:
        try:
            cursor_path.unlink()
        except FileNotFoundError:
            pass

    adapter = IbkrFileAdapterV0(
        repo_root=repo,
        positions_path=positions_path,
        open_orders_path=orders_path,
        events_jsonl_path=events_path if events_path.exists() else None,
        events_cursor_path=cursor_path,
    )
    adapter.connect()

    cp_path = repo / "args" / "data" / "control_plane.json"
    if not cp_path.exists():
        cp_path = repo / "control_plane.json"

    eng = Engine(
        repo_root=repo, run_id=run_id, control_plane_path=cp_path, broker=adapter
    )

    intent_id = f"adopt_{args.adopt_order_id}"
    eng.adopt_order(
        intent_id=intent_id,
        order_id=int(args.adopt_order_id),
        order=OrderSpec(
            symbol=args.adopt_symbol, side=args.adopt_side, qty=float(args.adopt_qty)
        ),
        client_order_id=f"oid_{args.adopt_order_id}",
    )

    print({"snapshot0": adapter.snapshot()})

    for _ in range(int(args.steps)):
        eng.step()
        t = eng.state.tickets[intent_id]
        if t.is_terminal():
            break

    t = eng.state.tickets[intent_id]
    print(
        {
            "run_dir": str(eng.run_dir),
            "intent_id": intent_id,
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
