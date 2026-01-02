from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .control_plane_v0 import ControlPlane
from .execution_v1 import BrokerAdapter, BrokerEvent, Engine, EventType, OrderIntent, OrderSpec


class FakeBroker(BrokerAdapter):
    """
    Sim broker for Stage5:
    - Schedules events based on per-order age since placement (not global tick).
    - This matches Engine.step() order: poll_events() happens before drain_actions().
    """

    def __init__(self, *, scenario: str, positions: Optional[Dict[str, float]] = None) -> None:
        self._connected = False
        self.scenario = scenario
        self.positions = dict(positions or {})
        self.orders: Dict[int, Dict[str, Any]] = {}
        self._events: List[BrokerEvent] = []
        self._tick = 0  # increments each poll_events()

    def connect(self) -> None:
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    def place_order(self, order_id: int, order: OrderSpec, client_order_id: str) -> None:
        # created_tick is the tick at the time of placement; age = current_tick - created_tick
        self.orders[order_id] = {
            "order_id": int(order_id),
            "client_order_id": client_order_id,
            "symbol": order.symbol,
            "side": order.side,
            "qty": float(order.qty),
            "status": "Submitted",
            "filled": 0.0,
            "remaining": float(order.qty),
            "created_tick": int(self._tick),
            "ack_sent": False,
        }

    def cancel_order(self, order_id: int) -> None:
        o = self.orders.get(int(order_id))
        if not o:
            return
        if o["status"] in {"Filled", "Cancelled", "Rejected"}:
            return
        o["status"] = "Cancelled"
        self._events.append(BrokerEvent(
            event_type=EventType.CANCELLED,
            order_id=int(order_id),
            client_order_id=str(o["client_order_id"]),
            symbol=str(o["symbol"]),
            reason="sim_cancelled",
        ))

    def replace_order(self, order_id: int, new_order: OrderSpec) -> None:
        o = self.orders.get(int(order_id))
        if not o:
            return
        if o["status"] in {"Filled", "Cancelled", "Rejected"}:
            return

        # Keep already-filled, adjust qty/remaining.
        o["qty"] = float(new_order.qty)
        o["remaining"] = max(0.0, float(new_order.qty) - float(o["filled"]))
        o["status"] = "Submitted"

        # Emit an ACK-like event for amend
        self._events.append(BrokerEvent(
            event_type=EventType.ACK,
            order_id=int(order_id),
            client_order_id=str(o["client_order_id"]),
            symbol=str(o["symbol"]),
        ))

    def poll_events(self) -> List[BrokerEvent]:
        self._tick += 1

        for oid, o in list(self.orders.items()):
            if o["status"] in {"Filled", "Cancelled", "Rejected"}:
                continue

            age = int(self._tick) - int(o.get("created_tick", 0))  # 1 means "first poll after placement"

            # REJECT scenario
            if self.scenario == "reject":
                if age == 1:
                    self._events.append(BrokerEvent(
                        event_type=EventType.REJECT,
                        order_id=int(oid),
                        client_order_id=str(o["client_order_id"]),
                        symbol=str(o["symbol"]),
                        reason="sim_reject",
                    ))
                    o["status"] = "Rejected"
                continue

            # Non-reject: send ACK once at age==1 (optional but useful)
            if age == 1 and not bool(o.get("ack_sent", False)):
                o["ack_sent"] = True
                self._events.append(BrokerEvent(
                    event_type=EventType.ACK,
                    order_id=int(oid),
                    client_order_id=str(o["client_order_id"]),
                    symbol=str(o["symbol"]),
                ))

            if self.scenario == "fill":
                if age == 2:
                    self._emit_fill(oid, o, float(o["remaining"]))

            elif self.scenario == "partial_then_fill":
                if age == 2:
                    part = max(1.0, float(o["qty"]) / 2.0)
                    part = min(part, float(o["remaining"]))
                    self._emit_fill(oid, o, part)
                if age == 3:
                    self._emit_fill(oid, o, float(o["remaining"]))

            elif self.scenario == "partial_then_cancel":
                if age == 2:
                    part = max(1.0, float(o["qty"]) / 2.0)
                    part = min(part, float(o["remaining"]))
                    self._emit_fill(oid, o, part)
                # cancel is driven by Engine.request_cancel()

            elif self.scenario == "replace_then_fill":
                if age == 2:
                    part = max(1.0, float(o["qty"]) / 2.0)
                    part = min(part, float(o["remaining"]))
                    self._emit_fill(oid, o, part)
                if age >= 3:
                    # fill whatever is left
                    self._emit_fill(oid, o, float(o["remaining"]))

        evs = list(self._events)
        self._events.clear()
        return evs

    def _emit_fill(self, oid: int, o: Dict[str, Any], fill_qty: float) -> None:
        fill_qty = max(0.0, min(float(fill_qty), float(o["remaining"])))
        if fill_qty <= 0:
            return

        o["filled"] += fill_qty
        o["remaining"] -= fill_qty

        sym = str(o["symbol"])
        side = str(o["side"]).upper()
        delta = fill_qty if side == "BUY" else -fill_qty
        self.positions[sym] = float(self.positions.get(sym, 0.0)) + delta

        remaining = float(o["remaining"])
        if remaining <= 0:
            o["status"] = "Filled"

        self._events.append(BrokerEvent(
            event_type=EventType.FILL,
            order_id=int(oid),
            client_order_id=str(o["client_order_id"]),
            symbol=sym,
            filled_qty=float(fill_qty),
            remaining_qty=max(0.0, remaining),
        ))

    def snapshot(self) -> Dict[str, Any]:
        open_orders = {}
        open_count = 0
        for oid, o in self.orders.items():
            status = str(o.get("status", ""))
            if status not in {"Filled", "Cancelled", "Rejected"}:
                open_count += 1
            open_orders[str(oid)] = dict(o)
        return {
            "connected": self._connected,
            "positions": dict(self.positions),
            "orders": open_orders,
            "open_orders_count": open_count,
        }


def _find_control_plane(repo: Path) -> Path:
    p1 = repo / "args" / "data" / "control_plane.json"
    if p1.exists():
        return p1
    p2 = repo / "control_plane.json"
    if p2.exists():
        return p2
    demo = repo / "control_plane.demo.json"
    ControlPlane(global_mode="ONLY_EXITS", allowlist=["MHG"]).save(demo)
    return demo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--scenario", required=True, choices=["fill", "reject", "partial_then_fill", "partial_then_cancel", "replace_then_fill"])
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()

    repo = Path(args.repo)
    cp_path = _find_control_plane(repo)
    run_id = args.run_id or f"demo_stage5_{args.scenario}_{int(time.time())}"

    broker = FakeBroker(scenario=args.scenario, positions={"AAPL": 2.0})
    eng = Engine(repo_root=repo, run_id=run_id, control_plane_path=cp_path, broker=broker)

    intent_id = "intent_aapl_exit_2"
    eng.submit_intent(OrderIntent(intent_id=intent_id, order=OrderSpec(symbol="AAPL", side="SELL", qty=2.0)))

    for _ in range(60):
        eng.step()
        t = eng.state.tickets[intent_id]

        if args.scenario == "partial_then_cancel" and t.state.value == "PARTIAL":
            eng.request_cancel(intent_id)

        if args.scenario == "replace_then_fill" and t.state.value == "PARTIAL":
            if t.filled_qty > 0:
                eng.request_replace_qty(intent_id, max(1.0, t.filled_qty))

        if t.is_terminal():
            break

    t = eng.state.tickets[intent_id]
    print({
        "run_dir": str(eng.run_dir),
        "terminal": t.terminal.value if t.terminal else None,
        "terminal_reason": t.terminal_reason,
        "final_position_aapl": broker.snapshot()["positions"].get("AAPL"),
        "counters": eng.state.counters,
        "reconcile_last_ratio": eng.state.reconcile_last_ratio,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
