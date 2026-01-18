from __future__ import annotations

import enum
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common_v0 import (
    append_jsonl,
    atomic_write_json,
    iter_jsonl,
    read_json,
    sha256_text,
    utc_now_iso,
)
from .control_plane_v0 import ControlPlane
from .policy_v0 import check_order_allowed


class EventType(str, enum.Enum):
    ACK = "ACK"
    REJECT = "REJECT"
    FILL = "FILL"  # partial/full; remaining_qty drives terminal
    CANCELLED = "CANCELLED"


class TicketState(str, enum.Enum):
    NEW = "NEW"
    PENDING_PLACE = "PENDING_PLACE"
    SENT = "SENT"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    REPLACE_REQUESTED = "REPLACE_REQUESTED"
    TERMINAL = "TERMINAL"


class TerminalState(str, enum.Enum):
    DONE = "DONE"  # exit completed -> flat
    FILLED = "FILLED"  # filled but not necessarily flat
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class OrderSpec:
    symbol: str
    side: str  # BUY/SELL
    qty: float
    order_type: str = "MKT"
    tif: str = "DAY"


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    order: OrderSpec
    created_at_utc: str = field(default_factory=utc_now_iso)

    def stable_client_order_id(self) -> str:
        payload = (
            f"{self.intent_id}|{self.order.symbol}|{self.order.side}|{self.order.qty}|"
            f"{self.order.order_type}|{self.order.tif}"
        )
        return sha256_text(payload)[:24]


@dataclass(frozen=True)
class BrokerEvent:
    event_type: EventType
    order_id: int
    client_order_id: str
    symbol: str
    filled_qty: float = 0.0  # delta fill
    remaining_qty: float = 0.0  # remaining after this fill
    reason: Optional[str] = None
    ts_utc: str = field(default_factory=utc_now_iso)


class BrokerAdapter:
    """Interface: real IBKR adapter will implement these methods."""

    def connect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def place_order(
        self, order_id: int, order: OrderSpec, client_order_id: str
    ) -> None: ...
    def cancel_order(self, order_id: int) -> None: ...
    def replace_order(self, order_id: int, new_order: OrderSpec) -> None: ...
    def poll_events(self) -> List[BrokerEvent]: ...
    def snapshot(self) -> Dict[str, Any]: ...


@dataclass
class Ticket:
    intent_id: str
    client_order_id: str
    order: OrderSpec
    order_id: Optional[int] = None

    state: TicketState = TicketState.NEW

    filled_qty: float = 0.0
    remaining_qty: float = 0.0

    last_event_ts_utc: Optional[str] = None

    terminal: Optional[TerminalState] = None
    terminal_reason: Optional[str] = None

    created_at_utc: str = field(default_factory=utc_now_iso)

    want_cancel: bool = False
    want_replace_qty: Optional[float] = None

    def is_terminal(self) -> bool:
        return self.state == TicketState.TERMINAL and self.terminal is not None


# ---------------------------
# FILL DEDUP (Stage5 v0)
# ---------------------------

FILL_DEDUP_MAX_KEYS_V0 = 2000  # keep small to avoid state/ledger bloat


def _norm_qty_key_v0(q: float) -> str:
    """
    Stable float-to-string for dedup keys (avoid 1 vs 1.0 mismatch).
    """
    try:
        x = float(q)
    except Exception:
        x = 0.0
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _fill_dedup_key_v0(ev: BrokerEvent) -> str:
    """
    Dedup key required by standard:
      order_id + ts_utc + reason + filled_qty
    """
    oid = str(int(ev.order_id))
    ts = str(ev.ts_utc or "1970-01-01T00:00:00Z")
    reason = (ev.reason or "").strip()
    qty = _norm_qty_key_v0(ev.filled_qty)
    return f"{oid}|{ts}|{reason}|{qty}"


@dataclass
class EngineState:
    schema: str = "stage5_engine_state_v1"
    run_id: str = ""
    created_at_utc: str = field(default_factory=utc_now_iso)

    next_order_id: int = 1000
    cooldown_until_epoch_s: float = 0.0

    tickets: Dict[str, Ticket] = field(default_factory=dict)

    # Persisted dedup keys (ordered, trimmed). Engine also maintains a set for O(1) membership.
    fill_dedup_keys_v0: List[str] = field(default_factory=list)

    counters: Dict[str, int] = field(
        default_factory=lambda: {
            "place_calls": 0,
            "cancel_calls": 0,
            "replace_calls": 0,
            "dedup_skips": 0,  # intent-level or general dedups
            "fill_dedup_skips": 0,  # FILL dedup skips
            "forbidden": 0,
            "events_seen": 0,
        }
    )

    reconcile_last_ratio: Optional[float] = None
    last_error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        def _ticket(t: Ticket) -> Dict[str, Any]:
            d = asdict(t)
            d["state"] = t.state.value
            d["terminal"] = t.terminal.value if t.terminal else None
            return d

        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "created_at_utc": self.created_at_utc,
            "next_order_id": self.next_order_id,
            "cooldown_until_epoch_s": self.cooldown_until_epoch_s,
            "tickets": {k: _ticket(v) for k, v in self.tickets.items()},
            "fill_dedup_keys_v0": list(self.fill_dedup_keys_v0),
            "counters": dict(self.counters),
            "reconcile_last_ratio": self.reconcile_last_ratio,
            "last_error": self.last_error,
        }

    @staticmethod
    def from_json(obj: Dict[str, Any]) -> "EngineState":
        st = EngineState()
        st.schema = str(obj.get("schema", st.schema))
        st.run_id = str(obj.get("run_id", ""))
        st.created_at_utc = str(obj.get("created_at_utc", utc_now_iso()))
        st.next_order_id = int(obj.get("next_order_id", 1000))
        st.cooldown_until_epoch_s = float(obj.get("cooldown_until_epoch_s", 0.0))

        st.counters = dict(obj.get("counters", st.counters))
        st.counters.setdefault("fill_dedup_skips", 0)
        st.counters.setdefault("dedup_skips", 0)

        st.reconcile_last_ratio = obj.get("reconcile_last_ratio", None)
        st.last_error = obj.get("last_error", None)

        fd = obj.get("fill_dedup_keys_v0", []) or []
        if isinstance(fd, list):
            st.fill_dedup_keys_v0 = [str(x) for x in fd if str(x).strip()]

        tickets_raw = obj.get("tickets", {}) or {}
        for intent_id, td in tickets_raw.items():
            t = Ticket(
                intent_id=str(td["intent_id"]),
                client_order_id=str(td["client_order_id"]),
                order=OrderSpec(**td["order"]),
                order_id=td.get("order_id", None),
                state=TicketState(str(td.get("state", "NEW"))),
                filled_qty=float(td.get("filled_qty", 0.0)),
                remaining_qty=float(td.get("remaining_qty", 0.0)),
                last_event_ts_utc=td.get("last_event_ts_utc", None),
                terminal=TerminalState(td["terminal"]) if td.get("terminal") else None,
                terminal_reason=td.get("terminal_reason", None),
                created_at_utc=str(td.get("created_at_utc", utc_now_iso())),
                want_cancel=bool(td.get("want_cancel", False)),
                want_replace_qty=td.get("want_replace_qty", None),
            )
            st.tickets[intent_id] = t
        return st


class Engine:
    def __init__(
        self,
        *,
        repo_root: Path,
        run_id: str,
        control_plane_path: Path,
        broker: BrokerAdapter,
    ) -> None:
        self.repo_root = repo_root
        self.control_plane_path = control_plane_path
        self.cp = ControlPlane.load(control_plane_path)
        self.run_id = run_id
        self.broker = broker

        self.run_dir = repo_root / self.cp.run_root / run_id
        self.state_path = self.run_dir / "state.json"
        self.ledger_path = self.run_dir / "ledger.jsonl"
        self.events_path = self.run_dir / "events.jsonl"
        self.reconcile_path = self.run_dir / "reconcile.jsonl"
        self.health_path = self.run_dir / "health.json"
        self.manifest_path = self.run_dir / "run_manifest.json"

        self.state = self._load_state()
        if not self.state.run_id:
            self.state.run_id = run_id

        # Ensure counters exist for older state files
        self.state.counters.setdefault("fill_dedup_skips", 0)
        self.state.counters.setdefault("dedup_skips", 0)
        self.state.counters.setdefault("events_seen", 0)

        # Build dedup set for O(1) membership; list in state preserves order and persists.
        self._fill_dedup_set_v0 = set(self.state.fill_dedup_keys_v0)

        if not self.manifest_path.exists():
            atomic_write_json(
                self.manifest_path,
                {
                    "schema": "run_manifest_v1",
                    "run_id": self.run_id,
                    "created_at_utc": utc_now_iso(),
                    "control_plane_path": str(self.control_plane_path),
                    "control_plane": self.cp.to_json(),
                },
            )

        if not self.broker.is_connected():
            self.broker.connect()

        self._handshake_with_snapshot()
        self._persist_state()

    def submit_intent(self, intent: OrderIntent) -> None:
        if intent.intent_id in self.state.tickets:
            self.state.counters["dedup_skips"] += 1
            return

        t = Ticket(
            intent_id=intent.intent_id,
            client_order_id=intent.stable_client_order_id(),
            order=intent.order,
            state=TicketState.PENDING_PLACE,
            remaining_qty=float(intent.order.qty),
        )
        self.state.tickets[intent.intent_id] = t
        append_jsonl(
            self.ledger_path,
            {
                "ts_utc": utc_now_iso(),
                "type": "INTENT_NEW",
                "ticket": self._ticket_json(t),
            },
        )
        self._persist_state()

    def adopt_order(
        self,
        *,
        intent_id: str,
        order_id: int,
        order: OrderSpec,
        client_order_id: Optional[str] = None,
        remaining_qty: Optional[float] = None,
    ) -> None:
        """
        Adopt an already-live broker order into the Engine WITHOUT placing anything.
        Useful for restart recovery / existing cleanup orders.
        """
        if intent_id in self.state.tickets:
            self.state.counters["dedup_skips"] += 1
            return

        coid = (client_order_id or f"oid_{int(order_id)}").strip()
        t = Ticket(
            intent_id=intent_id,
            client_order_id=coid,
            order=order,
            order_id=int(order_id),
            state=TicketState.SENT,
            remaining_qty=float(
                remaining_qty if remaining_qty is not None else order.qty
            ),
        )
        self.state.tickets[intent_id] = t
        append_jsonl(
            self.ledger_path,
            {
                "ts_utc": utc_now_iso(),
                "type": "ADOPT_ORDER",
                "ticket": self._ticket_json(t),
            },
        )
        self._persist_state()

    def request_cancel(self, intent_id: str) -> None:
        t = self.state.tickets.get(intent_id)
        if not t or t.is_terminal():
            return
        t.want_cancel = True
        append_jsonl(
            self.ledger_path,
            {
                "ts_utc": utc_now_iso(),
                "type": "INTENT_CANCEL_REQUEST",
                "intent_id": intent_id,
            },
        )
        self._persist_state()

    def request_replace_qty(self, intent_id: str, new_qty: float) -> None:
        t = self.state.tickets.get(intent_id)
        if not t or t.is_terminal():
            return
        t.want_replace_qty = float(new_qty)
        append_jsonl(
            self.ledger_path,
            {
                "ts_utc": utc_now_iso(),
                "type": "INTENT_REPLACE_REQUEST",
                "intent_id": intent_id,
                "new_qty": float(new_qty),
            },
        )
        self._persist_state()

    def step(self) -> None:
        stop_flag = (self.repo_root / self.cp.kill_switch_file).exists()
        safe_mode = (self.repo_root / self.cp.safe_mode_file).exists()

        try:
            events = self.broker.poll_events()
            for ev in events:
                self.state.counters["events_seen"] += 1
                append_jsonl(self.events_path, self._event_json(ev))
                self._apply_event(ev)

            self._apply_requests()

            if not stop_flag and not safe_mode:
                self._drain_actions()

            ratio, details = self._reconcile()
            self.state.reconcile_last_ratio = ratio
            append_jsonl(
                self.reconcile_path,
                {
                    "ts_utc": utc_now_iso(),
                    "schema": "reconcile_v1",
                    "ratio": ratio,
                    "details": details,
                },
            )

            atomic_write_json(
                self.health_path,
                {
                    "schema": "health_v1",
                    "ts_utc": utc_now_iso(),
                    "run_id": self.run_id,
                    "stop_flag": stop_flag,
                    "safe_mode": safe_mode,
                    "broker_connected": bool(self.broker.is_connected()),
                    "reconcile_ratio": ratio,
                    "counters": dict(self.state.counters),
                    "last_error": self.state.last_error,
                },
            )

        except Exception as e:
            self.state.last_error = f"{type(e).__name__}: {e}"
            self._set_cooldown("exception")
            append_jsonl(
                self.ledger_path,
                {
                    "ts_utc": utc_now_iso(),
                    "type": "ENGINE_EXCEPTION",
                    "error": self.state.last_error,
                },
            )
        finally:
            self._persist_state()

    # ---------------- internals ----------------

    def _load_state(self) -> EngineState:
        obj = read_json(self.state_path, default=None)
        if isinstance(obj, dict) and obj.get("schema") == "stage5_engine_state_v1":
            return EngineState.from_json(obj)

        st = EngineState()
        st.run_id = self.run_id
        for rec in iter_jsonl(self.ledger_path):
            if rec.get("type") == "STATE_SNAPSHOT":
                snap = rec.get("state")
                if isinstance(snap, dict):
                    st = EngineState.from_json(snap)
        return st

    def _persist_state(self) -> None:
        # state.fill_dedup_keys_v0 already updated in-place by _fill_dedup_seen_or_add_v0()
        atomic_write_json(self.state_path, self.state.to_json())
        append_jsonl(
            self.ledger_path,
            {
                "ts_utc": utc_now_iso(),
                "type": "STATE_SNAPSHOT",
                "state": self.state.to_json(),
            },
        )

    def _handshake_with_snapshot(self) -> None:
        snap = self.broker.snapshot() or {}
        orders = snap.get("orders", {}) or {}

        for t in self.state.tickets.values():
            if t.order_id is not None or t.is_terminal():
                continue

            for _, o in orders.items():
                coid = str(o.get("client_order_id", ""))
                if coid and coid == t.client_order_id:
                    try:
                        t.order_id = int(o.get("order_id"))
                    except Exception:
                        continue
                    t.state = TicketState.SENT
                    break

    def _set_cooldown(self, reason: str) -> None:
        until = time.time() + float(self.cp.cool_down_seconds)
        self.state.cooldown_until_epoch_s = max(
            self.state.cooldown_until_epoch_s, until
        )
        append_jsonl(
            self.ledger_path,
            {
                "ts_utc": utc_now_iso(),
                "type": "COOLDOWN_SET",
                "reason": reason,
                "until_epoch_s": self.state.cooldown_until_epoch_s,
            },
        )

    def _cooldown_active(self) -> bool:
        return time.time() < float(self.state.cooldown_until_epoch_s)

    def _apply_requests(self) -> None:
        for t in self.state.tickets.values():
            if t.is_terminal():
                continue
            if (
                t.want_cancel
                and t.order_id is not None
                and t.state != TicketState.CANCEL_REQUESTED
            ):
                t.state = TicketState.CANCEL_REQUESTED
            if (
                t.want_replace_qty is not None
                and t.order_id is not None
                and t.state != TicketState.REPLACE_REQUESTED
            ):
                t.state = TicketState.REPLACE_REQUESTED

    def _drain_actions(self) -> None:
        if self._cooldown_active():
            return

        snap = self.broker.snapshot() or {}
        positions = snap.get("positions", {}) or {}

        for t in self.state.tickets.values():
            if t.is_terminal():
                continue

            if t.state in {TicketState.PENDING_PLACE, TicketState.NEW}:
                pos_qty = float(positions.get(t.order.symbol, 0.0))
                dec = check_order_allowed(
                    self.cp,
                    symbol=t.order.symbol,
                    side=t.order.side,
                    qty=t.order.qty,
                    position_qty=pos_qty,
                    action="PLACE",
                )
                if not dec.allowed:
                    self.state.counters["forbidden"] += 1
                    t.state = TicketState.TERMINAL
                    t.terminal = TerminalState.FAILED
                    t.terminal_reason = f"forbidden:{dec.reason}"
                    append_jsonl(
                        self.ledger_path,
                        {
                            "ts_utc": utc_now_iso(),
                            "type": "FORBIDDEN",
                            "intent_id": t.intent_id,
                            "reason": dec.reason,
                        },
                    )
                    continue

                if t.order_id is not None:
                    self.state.counters["dedup_skips"] += 1
                    t.state = TicketState.SENT
                    continue

                oid = int(self.state.next_order_id)
                self.state.next_order_id += 1
                t.order_id = oid

                self.broker.place_order(oid, t.order, t.client_order_id)
                self.state.counters["place_calls"] += 1
                t.state = TicketState.SENT
                append_jsonl(
                    self.ledger_path,
                    {
                        "ts_utc": utc_now_iso(),
                        "type": "BROKER_PLACE",
                        "intent_id": t.intent_id,
                        "order_id": oid,
                        "client_order_id": t.client_order_id,
                    },
                )

            if t.state == TicketState.CANCEL_REQUESTED and t.order_id is not None:
                self.broker.cancel_order(int(t.order_id))
                self.state.counters["cancel_calls"] += 1
                append_jsonl(
                    self.ledger_path,
                    {
                        "ts_utc": utc_now_iso(),
                        "type": "BROKER_CANCEL",
                        "intent_id": t.intent_id,
                        "order_id": int(t.order_id),
                    },
                )

            if (
                t.state == TicketState.REPLACE_REQUESTED
                and t.order_id is not None
                and t.want_replace_qty is not None
            ):
                snap2 = self.broker.snapshot() or {}
                positions2 = snap2.get("positions", {}) or {}
                pos_qty2 = float(positions2.get(t.order.symbol, 0.0))

                new_order = OrderSpec(
                    symbol=t.order.symbol,
                    side=t.order.side,
                    qty=float(t.want_replace_qty),
                    order_type=t.order.order_type,
                    tif=t.order.tif,
                )
                dec2 = check_order_allowed(
                    self.cp,
                    symbol=t.order.symbol,
                    side=new_order.side,
                    qty=new_order.qty,
                    position_qty=pos_qty2,
                    action="REPLACE",
                )
                if not dec2.allowed:
                    self.state.counters["forbidden"] += 1
                    t.state = TicketState.TERMINAL
                    t.terminal = TerminalState.FAILED
                    t.terminal_reason = f"forbidden_replace:{dec2.reason}"
                    append_jsonl(
                        self.ledger_path,
                        {
                            "ts_utc": utc_now_iso(),
                            "type": "FORBIDDEN_REPLACE",
                            "intent_id": t.intent_id,
                            "reason": dec2.reason,
                        },
                    )
                    continue

                self.broker.replace_order(int(t.order_id), new_order)
                self.state.counters["replace_calls"] += 1
                t.order = new_order
                append_jsonl(
                    self.ledger_path,
                    {
                        "ts_utc": utc_now_iso(),
                        "type": "BROKER_REPLACE",
                        "intent_id": t.intent_id,
                        "order_id": int(t.order_id),
                        "new_qty": float(new_order.qty),
                    },
                )

    # ---------------------------
    # FILL DEDUP helpers
    # ---------------------------

    def _fill_dedup_seen_or_add_v0(self, ev: BrokerEvent) -> bool:
        """
        Returns True if duplicate fill should be dropped.
        Persists keys in state.fill_dedup_keys_v0 and maintains a set for O(1).
        """
        k = _fill_dedup_key_v0(ev)
        if k in self._fill_dedup_set_v0:
            self.state.counters["fill_dedup_skips"] += 1
            return True

        # add
        self._fill_dedup_set_v0.add(k)
        self.state.fill_dedup_keys_v0.append(k)

        # trim
        if len(self.state.fill_dedup_keys_v0) > FILL_DEDUP_MAX_KEYS_V0:
            overflow = len(self.state.fill_dedup_keys_v0) - FILL_DEDUP_MAX_KEYS_V0
            for _ in range(overflow):
                old = self.state.fill_dedup_keys_v0.pop(0)
                if old in self._fill_dedup_set_v0:
                    self._fill_dedup_set_v0.remove(old)
        return False

    def _apply_event(self, ev: BrokerEvent) -> None:
        # Apply dedup BEFORE mutating ticket, only for FILL
        if ev.event_type == EventType.FILL:
            if self._fill_dedup_seen_or_add_v0(ev):
                append_jsonl(
                    self.ledger_path,
                    {
                        "ts_utc": utc_now_iso(),
                        "type": "EVENT_FILL_DEDUP",
                        "order_id": ev.order_id,
                        "client_order_id": ev.client_order_id,
                        "reason": ev.reason,
                        "filled_qty": ev.filled_qty,
                        "ts_event_utc": ev.ts_utc,
                    },
                )
                return

        t: Optional[Ticket] = None

        for tt in self.state.tickets.values():
            if tt.client_order_id == ev.client_order_id:
                t = tt
                break

        if t is None:
            for tt in self.state.tickets.values():
                if tt.order_id == ev.order_id:
                    t = tt
                    break

        if t is None:
            append_jsonl(
                self.ledger_path,
                {
                    "ts_utc": utc_now_iso(),
                    "type": "EVENT_ORPHAN",
                    "event": self._event_json(ev),
                },
            )
            return

        t.last_event_ts_utc = ev.ts_utc

        if ev.event_type == EventType.ACK:
            if not t.is_terminal() and t.state in {
                TicketState.SENT,
                TicketState.PENDING_PLACE,
                TicketState.NEW,
            }:
                t.state = TicketState.ACKED
            append_jsonl(
                self.ledger_path,
                {
                    "ts_utc": utc_now_iso(),
                    "type": "EVENT_ACK",
                    "intent_id": t.intent_id,
                    "order_id": ev.order_id,
                },
            )

        elif ev.event_type == EventType.REJECT:
            if not t.is_terminal():
                t.state = TicketState.TERMINAL
                t.terminal = TerminalState.FAILED
                t.terminal_reason = ev.reason or "reject"
                append_jsonl(
                    self.ledger_path,
                    {
                        "ts_utc": utc_now_iso(),
                        "type": "EVENT_REJECT",
                        "intent_id": t.intent_id,
                        "order_id": ev.order_id,
                        "reason": t.terminal_reason,
                    },
                )
                self._set_cooldown("reject")

        elif ev.event_type == EventType.CANCELLED:
            if not t.is_terminal():
                t.state = TicketState.TERMINAL
                t.terminal = TerminalState.CANCELLED
                t.terminal_reason = ev.reason or "cancelled"
                append_jsonl(
                    self.ledger_path,
                    {
                        "ts_utc": utc_now_iso(),
                        "type": "EVENT_CANCELLED",
                        "intent_id": t.intent_id,
                        "order_id": ev.order_id,
                    },
                )

        elif ev.event_type == EventType.FILL:
            if t.is_terminal():
                return

            t.filled_qty += float(ev.filled_qty)
            t.remaining_qty = float(ev.remaining_qty)

            if t.remaining_qty > 0:
                t.state = TicketState.PARTIAL
                append_jsonl(
                    self.ledger_path,
                    {
                        "ts_utc": utc_now_iso(),
                        "type": "EVENT_PARTIAL_FILL",
                        "intent_id": t.intent_id,
                        "order_id": ev.order_id,
                        "filled_total": t.filled_qty,
                        "remaining": t.remaining_qty,
                        "reason": ev.reason,
                    },
                )
            else:
                snap = self.broker.snapshot() or {}
                pos = float((snap.get("positions", {}) or {}).get(t.order.symbol, 0.0))
                term = TerminalState.DONE if abs(pos) < 1e-9 else TerminalState.FILLED
                t.state = TicketState.TERMINAL
                t.terminal = term
                t.terminal_reason = "filled"
                append_jsonl(
                    self.ledger_path,
                    {
                        "ts_utc": utc_now_iso(),
                        "type": "EVENT_FILL_TERMINAL",
                        "intent_id": t.intent_id,
                        "order_id": ev.order_id,
                        "terminal": term.value,
                        "pos": pos,
                        "reason": ev.reason,
                    },
                )

    def _reconcile(self) -> Tuple[float, Dict[str, Any]]:
        """
        Reconcile tickets vs broker snapshot.

        IMPORTANT for IBKR: open orders may not contain client_order_id (empty).
        We therefore support fallback matching by order_id when ticket.order_id is present.
        """
        snap = self.broker.snapshot() or {}
        positions = snap.get("positions", {}) or {}
        orders = snap.get("orders", {}) or {}

        open_client_ids = set()
        open_order_ids = set()

        for _, o in (orders or {}).items():
            oid = o.get("order_id", o.get("orderId", None))
            try:
                if oid is not None:
                    open_order_ids.add(int(oid))
            except Exception:
                pass

            coid = str(o.get("client_order_id", "") or "")
            if coid:
                open_client_ids.add(coid)

        checks = 0
        ok = 0
        mismatches: List[Dict[str, Any]] = []

        for t in self.state.tickets.values():
            if t.is_terminal():
                checks += 1
                ok += 1
                continue

            checks += 1
            matched = False

            if t.client_order_id and t.client_order_id in open_client_ids:
                matched = True
            elif t.order_id is not None and int(t.order_id) in open_order_ids:
                matched = True

            if matched:
                ok += 1
            else:
                mismatches.append(
                    {
                        "type": "missing_open_order",
                        "intent_id": t.intent_id,
                        "client_order_id": t.client_order_id,
                        "order_id": t.order_id,
                        "state": t.state.value,
                    }
                )

        for t in self.state.tickets.values():
            if t.is_terminal() and t.terminal == TerminalState.DONE:
                checks += 1
                pos = float(positions.get(t.order.symbol, 0.0))
                if abs(pos) < 1e-9:
                    ok += 1
                else:
                    mismatches.append(
                        {
                            "type": "not_flat_after_done",
                            "intent_id": t.intent_id,
                            "symbol": t.order.symbol,
                            "pos": pos,
                        }
                    )

        ratio = 1.0 if checks == 0 else (ok / checks)
        details = {
            "checks": checks,
            "ok": ok,
            "mismatches": mismatches[:50],
            "open_orders_count": int(
                snap.get("open_orders_count", len(open_order_ids))
            ),
            "positions_count": int(len(positions)),
            "open_order_ids_count": int(len(open_order_ids)),
            "open_client_ids_count": int(len(open_client_ids)),
        }
        return ratio, details

    def _ticket_json(self, t: Ticket) -> Dict[str, Any]:
        return {
            "intent_id": t.intent_id,
            "client_order_id": t.client_order_id,
            "order_id": t.order_id,
            "order": asdict(t.order),
            "state": t.state.value,
            "filled_qty": t.filled_qty,
            "remaining_qty": t.remaining_qty,
            "terminal": t.terminal.value if t.terminal else None,
            "terminal_reason": t.terminal_reason,
            "created_at_utc": t.created_at_utc,
            "last_event_ts_utc": t.last_event_ts_utc,
            "want_cancel": t.want_cancel,
            "want_replace_qty": t.want_replace_qty,
        }

    def _event_json(self, ev: BrokerEvent) -> Dict[str, Any]:
        return {
            "ts_utc": ev.ts_utc,
            "event_type": ev.event_type.value,
            "order_id": ev.order_id,
            "client_order_id": ev.client_order_id,
            "symbol": ev.symbol,
            "filled_qty": ev.filled_qty,
            "remaining_qty": ev.remaining_qty,
            "reason": ev.reason,
        }
