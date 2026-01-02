from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common_v0 import atomic_write_json, read_json
from .execution_v1 import BrokerAdapter, BrokerEvent, OrderSpec, EventType
from .ibkr_event_normalizer_v0 import normalize_ibkr_record


def _read_json_best_effort(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_positions_snapshot(path: Path) -> Dict[str, float]:
    """
    Supports:
      - {"schema":"...","rows":[{"symbol":"AAPL","position":2.0}, ...]}
      - [{"symbol":"AAPL","position":2.0}, ...]
      - {"positions":{"AAPL":2.0}}
    """
    obj = _read_json_best_effort(path)
    if obj is None:
        return {}
    if isinstance(obj, dict):
        if isinstance(obj.get("positions"), dict):
            return {str(k): float(v) for k, v in obj["positions"].items()}
        rows = obj.get("rows")
        if isinstance(rows, list):
            out: Dict[str, float] = {}
            for r in rows:
                if isinstance(r, dict) and "symbol" in r and "position" in r:
                    out[str(r["symbol"])] = float(r["position"])
            return out
    if isinstance(obj, list):
        out: Dict[str, float] = {}
        for r in obj:
            if isinstance(r, dict) and "symbol" in r and "position" in r:
                out[str(r["symbol"])] = float(r["position"])
        return out
    return {}


def parse_open_orders_snapshot(path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Supports:
      - {"schema":"...","rows":[{"orderId":1018,"symbol":"AAPL","status":"PreSubmitted",...}, ...]}
      - [{"orderId":1018,...}, ...]
      - {"orders":{ "1018": {...} }}
    """
    obj = _read_json_best_effort(path)
    if obj is None:
        return {}
    if isinstance(obj, dict):
        if isinstance(obj.get("orders"), dict):
            out: Dict[str, Dict[str, Any]] = {}
            for k, v in obj["orders"].items():
                if isinstance(v, dict):
                    out[str(k)] = dict(v)
            return out
        rows = obj.get("rows")
        if isinstance(rows, list):
            out: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                oid = r.get("orderId", r.get("order_id"))
                if oid is None:
                    continue
                out[str(int(oid))] = dict(r)
            return out
    if isinstance(obj, list):
        out: Dict[str, Dict[str, Any]] = {}
        for r in obj:
            if not isinstance(r, dict):
                continue
            oid = r.get("orderId", r.get("order_id"))
            if oid is None:
                continue
            out[str(int(oid))] = dict(r)
        return out
    return {}


class JsonlTailer:
    """Byte-offset tailer with persisted cursor (restart-safe)."""
    def __init__(self, path: Path, cursor_path: Path) -> None:
        self.path = path
        self.cursor_path = cursor_path
        self.cursor = 0

        st = read_json(cursor_path, default={})
        if isinstance(st, dict) and st.get("path") == str(path):
            try:
                self.cursor = int(st.get("cursor", 0))
            except Exception:
                self.cursor = 0

    def poll(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out: List[Dict[str, Any]] = []
        with open(self.path, "rb") as f:
            f.seek(self.cursor)
            while True:
                line = f.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line.decode("utf-8").strip())
                    if isinstance(rec, dict):
                        out.append(rec)
                except Exception:
                    pass
            self.cursor = f.tell()

        atomic_write_json(self.cursor_path, {"path": str(self.path), "cursor": self.cursor})
        return out


class IbkrFileAdapterV0(BrokerAdapter):
    """
    READ-ONLY adapter:
      - snapshot(): from json snapshot files (positions/open orders)
      - poll_events(): from jsonl events file (optional), with caches:
          * remaining cache from ORDER_STATUS
          * symbol cache from OPEN_ORDER
      - place/cancel/replace: forbidden (raises)
    """

    def __init__(
        self,
        *,
        repo_root: Path,
        positions_path: Path,
        open_orders_path: Path,
        events_jsonl_path: Optional[Path] = None,
        events_cursor_path: Optional[Path] = None,
    ) -> None:
        self.repo_root = repo_root
        self.positions_path = positions_path
        self.open_orders_path = open_orders_path
        self.events_jsonl_path = events_jsonl_path
        self._connected = False

        # Caches (for patching EXEC_DETAILS + missing symbol)
        self._last_remaining_by_order_id: Dict[int, float] = {}
        self._last_symbol_by_order_id: Dict[int, str] = {}

        self._tailer: Optional[JsonlTailer] = None
        if events_jsonl_path is not None:
            cursor = events_cursor_path or (repo_root / "args" / "data" / "ibkr_events.cursor.json")
            self._tailer = JsonlTailer(events_jsonl_path, cursor)

    def connect(self) -> None:
        self._connected = True

    def is_connected(self) -> bool:
        return self._connected

    def place_order(self, order_id: int, order: OrderSpec, client_order_id: str) -> None:
        raise RuntimeError("IbkrFileAdapterV0 is READ-ONLY (place_order forbidden)")

    def cancel_order(self, order_id: int) -> None:
        raise RuntimeError("IbkrFileAdapterV0 is READ-ONLY (cancel_order forbidden)")

    def replace_order(self, order_id: int, new_order: OrderSpec) -> None:
        raise RuntimeError("IbkrFileAdapterV0 is READ-ONLY (replace_order forbidden)")

    def poll_events(self) -> List[BrokerEvent]:
        if self._tailer is None:
            return []
        recs = self._tailer.poll()
        out: List[BrokerEvent] = []

        # Pass 1: update caches
        for r in recs:
            typ = str(r.get("type", "")).upper()

            if typ == "ORDER_STATUS":
                try:
                    oid = int(r.get("orderId"))
                    rem = float(r.get("remaining", 0.0))
                    self._last_remaining_by_order_id[oid] = rem
                except Exception:
                    pass

            if typ == "OPEN_ORDER":
                try:
                    oid = int(r.get("orderId"))
                    sym = str(r.get("symbol", "") or "")
                    if sym:
                        self._last_symbol_by_order_id[oid] = sym
                except Exception:
                    pass

        # Pass 2: normalize + patch
        for r in recs:
            ev = normalize_ibkr_record(r)
            if ev is None:
                continue

            typ = str(r.get("type", "")).upper()

            # Patch symbol if missing
            if not ev.symbol:
                try:
                    sym = self._last_symbol_by_order_id.get(int(ev.order_id), "")
                    if sym:
                        ev = BrokerEvent(
                            ev.event_type,
                            ev.order_id,
                            ev.client_order_id,
                            sym,
                            ev.filled_qty,
                            ev.remaining_qty,
                            ev.reason,
                            ev.ts_utc,
                        )
                except Exception:
                    pass

            # If this is EXEC_DETAILS delta fill: patch remaining from last ORDER_STATUS if available
            if typ == "EXEC_DETAILS":
                try:
                    rem = self._last_remaining_by_order_id.get(int(ev.order_id))
                    if rem is not None:
                        ev = BrokerEvent(
                            ev.event_type,
                            ev.order_id,
                            ev.client_order_id,
                            ev.symbol,
                            ev.filled_qty,
                            float(rem),
                            ev.reason,
                            ev.ts_utc,
                        )
                except Exception:
                    pass

            # If this is ORDER_STATUS Filled and normalizer returned FILL with non-zero filled_qty:
            # force filled_qty=0 to avoid double-count (terminal marker only).
            if typ == "ORDER_STATUS":
                st = str(r.get("status", "")).lower()
                if st == "filled" and ev.event_type == EventType.FILL and ev.filled_qty != 0.0:
                    ev = BrokerEvent(
                        ev.event_type,
                        ev.order_id,
                        ev.client_order_id,
                        ev.symbol,
                        0.0,
                        0.0,
                        ev.reason or "filled",
                        ev.ts_utc,
                    )

            out.append(ev)

        return out

    def snapshot(self) -> Dict[str, Any]:
        pos = parse_positions_snapshot(self.positions_path)
        orders_raw = parse_open_orders_snapshot(self.open_orders_path)

        orders: Dict[str, Dict[str, Any]] = {}
        open_cnt = 0

        for k, o in orders_raw.items():
            if not isinstance(o, dict):
                continue

            oid = o.get("orderId", o.get("order_id", k))
            try:
                oid_int = int(oid)
            except Exception:
                continue

            status = str(o.get("status", o.get("orderStatus", "Submitted")))
            sym = str(o.get("symbol", o.get("localSymbol", "")))
            side = str(o.get("side", o.get("action", "")))

            qty = o.get("qty", o.get("totalQuantity", 0.0))
            filled = o.get("filled", o.get("filledQuantity", 0.0))
            remaining = o.get("remaining", o.get("remainingQuantity", None))

            if remaining is None:
                try:
                    remaining = float(qty) - float(filled)
                except Exception:
                    remaining = 0.0

            if status not in {"Filled", "Cancelled", "Canceled", "Rejected"}:
                open_cnt += 1

            orders[str(oid_int)] = {
                "order_id": oid_int,
                "client_order_id": o.get("client_order_id", o.get("clientOrderId", "")),
                "symbol": sym,
                "side": side,
                "qty": float(qty) if qty is not None else 0.0,
                "status": status,
                "filled": float(filled) if filled is not None else 0.0,
                "remaining": float(remaining) if remaining is not None else 0.0,
            }

        return {
            "connected": self._connected,
            "positions": pos,
            "orders": orders,
            "open_orders_count": open_cnt,
        }
