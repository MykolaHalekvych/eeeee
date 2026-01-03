from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import uuid
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order


EXIT_OK = 0
EXIT_FAIL = 1
EXIT_EXEC_DISABLED = 2


WARN_CODES = {399}
INFO_CODES = set()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{uuid.uuid4().hex[:8]}"


def _read_json(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig")
    return json.loads(raw)


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _one_line(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def sanitize_order_v0(order: Order) -> Dict[str, Any]:
    """
    Bulletproof sanitizer for ibapi Order:
      - force eTradeOnly/firmQuoteOnly False when possible
      - remove nbboPriceCap ONLY if present in __dict__ (safe across ibapi variants)
    Never raises.
    """
    changes: Dict[str, Any] = {}
    d = getattr(order, "__dict__", None)

    # Remove deprecated/invalid attribute safely
    try:
        if isinstance(d, dict) and "nbboPriceCap" in d:
            prev = d.get("nbboPriceCap")
            d.pop("nbboPriceCap", None)
            changes["nbboPriceCap"] = {"prev": prev, "deleted": True}
    except Exception:
        pass

    for k in ("eTradeOnly", "firmQuoteOnly"):
        try:
            prev = getattr(order, k) if hasattr(order, k) else None
            setattr(order, k, False)
            changes[k] = {"prev": prev, "new": False}
        except Exception:
            try:
                if isinstance(d, dict):
                    prev = d.get(k)
                    d[k] = False
                    changes[k] = {"prev": prev, "new": False, "via": "__dict__"}
            except Exception:
                pass

    return changes


def _contract_from_any(obj: Dict[str, Any]) -> Dict[str, Any]:
    if "contract" in obj and isinstance(obj["contract"], dict):
        return obj["contract"]
    return obj


def _build_contract_from_item(it: Dict[str, Any]) -> Contract:
    c = Contract()
    c.conId = int(it.get("conId") or 0)
    c.symbol = str(it.get("symbol") or "")
    c.secType = str(it.get("secType") or "")
    c.currency = str(it.get("currency") or "USD")

    exch = str(it.get("exchange") or "")
    if c.secType.upper() == "STK":
        c.exchange = "SMART"
    else:
        c.exchange = exch or "SMART"

    ltd = str(it.get("lastTradeDateOrContractMonth") or "")
    if c.secType.upper() == "FUT" and ltd:
        c.lastTradeDateOrContractMonth = ltd

    c.localSymbol = str(it.get("localSymbol") or "")
    return c


@dataclass
class GateResult:
    ok: bool
    exit_code: int
    reason: str


def _stop_flag_path(repo: Path) -> Path:
    return repo / "args" / "logs" / "stop.flag"


def _gate(cp: Dict[str, Any], scenario: str, repo: Path, confirm_paper: bool) -> GateResult:
    if not _stop_flag_path(repo).exists():
        return GateResult(False, EXIT_EXEC_DISABLED, f"STOP_FLAG_REQUIRED: create {_stop_flag_path(repo)}")

    execution_mode = _u(cp.get("execution_mode"))
    global_mode = _u(cp.get("global_mode"))
    enable_paper = bool(cp.get("enable_paper_execution", False))

    can_execute = (execution_mode == "PAPER") and enable_paper and bool(confirm_paper)
    if not can_execute:
        return GateResult(False, EXIT_EXEC_DISABLED, "EXECUTION_DISABLED")

    # extra safety: non-exit test orders only under HALT
    if scenario in ("scenario_cancelled_v1", "scenario_rejected_v1"):
        if global_mode and global_mode != "HALT":
            return GateResult(False, EXIT_EXEC_DISABLED, f"GLOBAL_MODE_NOT_SAFE_FOR_TEST: {global_mode} (need HALT)")
    else:
        # fill is usually exit-only; allow ONLY_EXITS or HALT here; we will enforce HALT later if roundtrip is used
        if global_mode and global_mode not in ("ONLY_EXITS", "HALT"):
            return GateResult(False, EXIT_EXEC_DISABLED, f"GLOBAL_MODE_NOT_SAFE_FOR_EXIT: {global_mode} (need ONLY_EXITS/HALT)")

    return GateResult(True, EXIT_OK, "OK")


class _App(EWrapper, EClient):
    def __init__(self, events_jsonl: Path) -> None:
        EClient.__init__(self, self)
        self._cv = threading.Condition()
        self._next_valid_id_evt = threading.Event()
        self.events_jsonl = events_jsonl

        self.next_order_id: Optional[int] = None
        self.order_errors: Dict[int, List[Dict[str, Any]]] = {}
        self.order_status: Dict[int, List[Dict[str, Any]]] = {}
        self.open_orders: Dict[int, List[Dict[str, Any]]] = {}
        self.exec_details: Dict[int, List[Dict[str, Any]]] = {}

    def _emit(self, ev: str, payload: Dict[str, Any]) -> None:
        rec = {"ts_utc": _utc_now_iso(), "event": ev, **payload}
        _append_jsonl(self.events_jsonl, rec)

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.next_order_id = int(orderId)
        self._next_valid_id_evt.set()
        with self._cv:
            self._cv.notify_all()
        self._emit("nextValidId", {"orderId": int(orderId)})

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        rec = {"reqId": int(reqId), "code": int(errorCode), "msg": str(errorString), "advanced": str(advancedOrderRejectJson or "")}
        if reqId > 0:
            self.order_errors.setdefault(int(reqId), []).append(rec)
        self._emit("error", {"orderId": int(reqId), **rec})
        with self._cv:
            self._cv.notify_all()

    def openOrder(self, orderId, contract, order, orderState) -> None:  # noqa: N802
        rec = {"orderId": int(orderId), "permId": int(getattr(order, "permId", 0) or 0), "status": str(getattr(orderState, "status", "") or "")}
        self.open_orders.setdefault(int(orderId), []).append(rec)
        self._emit("openOrder", rec)
        with self._cv:
            self._cv.notify_all()

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice) -> None:  # noqa: N802,E501
        rec = {
            "orderId": int(orderId),
            "status": str(status),
            "filled": float(filled),
            "remaining": float(remaining),
            "avgFillPrice": float(avgFillPrice),
            "permId": int(permId or 0),
            "lastFillPrice": float(lastFillPrice),
        }
        self.order_status.setdefault(int(orderId), []).append(rec)
        self._emit("orderStatus", rec)
        with self._cv:
            self._cv.notify_all()

    def execDetails(self, reqId, contract, execution) -> None:  # noqa: N802
        oid = int(getattr(execution, "orderId", 0) or 0)
        rec = {
            "orderId": oid,
            "execId": str(getattr(execution, "execId", "") or ""),
            "shares": float(getattr(execution, "shares", 0.0) or 0.0),
            "price": float(getattr(execution, "price", 0.0) or 0.0),
            "time": str(getattr(execution, "time", "") or ""),
        }
        if oid > 0:
            self.exec_details.setdefault(oid, []).append(rec)
        self._emit("execDetails", rec)
        with self._cv:
            self._cv.notify_all()

    def wait_next_valid_id(self, timeout_s: float) -> None:
        if not self._next_valid_id_evt.wait(timeout=timeout_s):
            raise RuntimeError("TIMEOUT_WAIT_NEXT_VALID_ID")

    def wait_first_signal(self, order_id: int, timeout_s: float) -> Dict[str, Any]:
        end = time.time() + float(timeout_s)
        with self._cv:
            while time.time() < end:
                if order_id in self.order_status:
                    break
                if order_id in self.open_orders:
                    break
                if order_id in self.order_errors:
                    break
                remain = end - time.time()
                if remain <= 0:
                    break
                self._cv.wait(timeout=remain)

        return {
            "orderStatus": (self.order_status.get(order_id) or [None])[-1],
            "openOrder": (self.open_orders.get(order_id) or [None])[-1],
            "errors": self.order_errors.get(order_id, []),
            "execDetails": self.exec_details.get(order_id, []),
        }

    def wait_until(self, order_id: int, timeout_s: float, predicate) -> Dict[str, Any]:
        end = time.time() + float(timeout_s)
        with self._cv:
            while time.time() < end:
                snap = {
                    "orderStatus": (self.order_status.get(order_id) or [None])[-1],
                    "openOrder": (self.open_orders.get(order_id) or [None])[-1],
                    "errors": self.order_errors.get(order_id, []),
                    "execDetails": self.exec_details.get(order_id, []),
                }
                if predicate(snap):
                    return snap
                remain = end - time.time()
                if remain <= 0:
                    break
                self._cv.wait(timeout=min(0.5, remain))
        return {
            "orderStatus": (self.order_status.get(order_id) or [None])[-1],
            "openOrder": (self.open_orders.get(order_id) or [None])[-1],
            "errors": self.order_errors.get(order_id, []),
            "execDetails": self.exec_details.get(order_id, []),
        }


def _classify_order_errors(errs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    warns: List[Dict[str, Any]] = []
    fails: List[Dict[str, Any]] = []
    for e in errs:
        code = int(e.get("code") or 0)
        if code in INFO_CODES:
            continue
        if code in WARN_CODES:
            warns.append(e)
        else:
            fails.append(e)
    return warns, fails


def _safe_cancel(app: _App, order_id: int) -> None:
    try:
        app.cancelOrder(int(order_id))
    except TypeError:
        app.cancelOrder(int(order_id), "")


def _default_contract_candidates(repo: Path) -> List[Path]:
    data_dir = repo / "args" / "data"
    if not data_dir.exists():
        return []
    return sorted(data_dir.glob("ibkr*contract*.json"))


def _load_contract_item(repo: Path, contract_json: Optional[str], symbol_hint: str) -> Dict[str, Any]:
    if contract_json:
        p = (repo / contract_json).resolve() if not Path(contract_json).is_absolute() else Path(contract_json)
        obj = _read_json(p)
        return _contract_from_any(obj)

    cands = _default_contract_candidates(repo)
    for p in cands:
        try:
            obj = _read_json(p)
            it = _contract_from_any(obj)
            sym = str(it.get("symbol") or "")
            if symbol_hint and sym.upper() == symbol_hint.upper():
                return it
        except Exception:
            continue

    if cands:
        for p in cands:
            try:
                obj = _read_json(p)
                return _contract_from_any(obj)
            except Exception:
                continue

    raise RuntimeError("CONTRACT_JSON_NOT_FOUND: provide --contract-json")


def _load_control_plane(repo: Path, control_plane_rel: str) -> Dict[str, Any]:
    cp_path = (repo / control_plane_rel).resolve()
    return _read_json(cp_path)


def _resolve_conn(cp: Dict[str, Any], host: Optional[str], port: Optional[int], client_id: Optional[int]) -> Tuple[str, int, int]:
    h = host or str(cp.get("ib_host") or cp.get("host") or "localhost")
    p = int(port or cp.get("port") or 7497)
    cid = int(client_id or cp.get("client_id") or 79)
    return h, p, cid


def _load_positions_snapshot(repo: Path) -> Optional[Dict[str, Any]]:
    p = repo / "args" / "data" / "ibkr_positions_snapshot_v0.json"
    if p.exists():
        try:
            return _read_json(p)
        except Exception:
            return None
    return None


def _extract_pos_qty(positions_obj: Dict[str, Any], symbol: str) -> float:
    rows = positions_obj.get("rows") or []
    sym_u = symbol.upper()
    for r in rows:
        if str(r.get("symbol") or "").upper() == sym_u:
            return float(r.get("position") or 0.0)
    return 0.0


def _is_filled(snap: Dict[str, Any]) -> bool:
    st = ""
    if snap.get("orderStatus"):
        st = str(snap["orderStatus"].get("status", "") or "")
    if st.lower().startswith("fill"):
        return True
    if snap.get("execDetails") and len(snap["execDetails"]) > 0:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--scenario", required=True, choices=["scenario_cancelled_v1", "scenario_rejected_v1", "scenario_fill_v1"])
    ap.add_argument("--control-plane", default="args/data/control_plane.json")

    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--client-id", type=int, default=None)

    ap.add_argument("--contract-json", default=None)
    ap.add_argument("--symbol", default="", help="Symbol hint (e.g. MHG)")

    ap.add_argument("--confirm-paper", action="store_true")
    ap.add_argument("--confirm-fill", default="NO", help="Must be YES for scenario_fill_v1")
    ap.add_argument("--confirm-roundtrip", default="NO", help="Must be YES to allow roundtrip when position==0")
    ap.add_argument("--roundtrip-qty", type=float, default=1.0)

    ap.add_argument("--listen-after-place-s", type=float, default=2.5)
    ap.add_argument("--cancel-wait-s", type=float, default=10.0)
    ap.add_argument("--fill-wait-s", type=float, default=25.0)

    ap.add_argument("--lmt-price", type=float, default=None)
    ap.add_argument("--qty", type=float, default=1.0)

    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    run_id = _run_id()
    scenario = args.scenario

    evidence_dir = repo / "args" / "ops_evidence" / "terminal" / run_id / scenario
    evidence_dir.mkdir(parents=True, exist_ok=True)

    cp = _load_control_plane(repo, args.control_plane)
    _write_json(evidence_dir / "control_plane_snapshot.json", cp)

    gate = _gate(cp, scenario, repo, confirm_paper=bool(args.confirm_paper))
    if not gate.ok:
        out = {
            "schema": "terminal_scenario_v1",
            "scenario": scenario,
            "run_id": run_id,
            "ok": False,
            "exit_code": gate.exit_code,
            "reason": gate.reason,
            "execution_mode": _u(cp.get("execution_mode")),
            "global_mode": _u(cp.get("global_mode")),
            "enable_paper_execution": bool(cp.get("enable_paper_execution", False)),
            "stop_flag": str(_stop_flag_path(repo)),
            "evidence_dir": str(evidence_dir),
        }
        _write_json(evidence_dir / "terminal_result.json", out)
        _one_line(out)
        return gate.exit_code

    contract_item = _load_contract_item(repo, args.contract_json, args.symbol)
    _write_json(evidence_dir / "contract_item.json", contract_item)
    contract_obj = _build_contract_from_item(contract_item)

    host, port, client_id = _resolve_conn(cp, args.host, args.port, args.client_id)

    events_jsonl = evidence_dir / "ib_events.jsonl"
    app = _App(events_jsonl)

    app.connect(host, port, client_id)
    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    try:
        app.wait_next_valid_id(timeout_s=12.0)
        base_oid = int(app.next_order_id or 0)
        if base_oid <= 0:
            raise RuntimeError("NO_NEXT_ORDER_ID")

        order_spec: Dict[str, Any] = {"scenario": scenario}
        terminal_state = "UNKNOWN"

        if scenario == "scenario_cancelled_v1":
            oid = base_oid
            o = Order()
            o.action = "BUY"
            o.orderType = "LMT"
            o.totalQuantity = float(args.qty or 1.0)
            o.tif = "DAY"
            o.lmtPrice = float(args.lmt_price if args.lmt_price is not None else 1.0)
            san = sanitize_order_v0(o)

            order_spec.update({"action": o.action, "type": o.orderType, "qty": o.totalQuantity, "lmtPrice": o.lmtPrice, "sanitized": san})
            _write_json(evidence_dir / "order_spec.json", order_spec)

            app.placeOrder(oid, contract_obj, o)
            snap0 = app.wait_first_signal(oid, timeout_s=float(args.listen_after_place_s))
            warns, fails = _classify_order_errors(list(snap0.get("errors") or []))
            if fails:
                raise RuntimeError(f"UNEXPECTED_REJECT_ON_CANCELLED: {fails[:1]}")

            _safe_cancel(app, oid)
            snap1 = app.wait_until(
                oid,
                timeout_s=float(args.cancel_wait_s),
                predicate=lambda s: (
                    s.get("orderStatus")
                    and str(s["orderStatus"].get("status", "")).lower().startswith("cancel")
                ) or (s.get("errors") and len(_classify_order_errors(s["errors"])[1]) > 0),
            )
            warns1, fails1 = _classify_order_errors(list(snap1.get("errors") or []))
            st = (snap1.get("orderStatus") or {}).get("status") if snap1.get("orderStatus") else None
            if fails1:
                raise RuntimeError(f"CANCELLED_FAILED_ERRORS: {fails1[:1]}")
            if not st or not str(st).lower().startswith("cancel"):
                raise RuntimeError(f"CANCELLED_NOT_CONFIRMED: orderStatus={snap1.get('orderStatus')}")

            terminal_state = "CANCELLED"
            out_order_ids = [oid]

        elif scenario == "scenario_rejected_v1":
            oid = base_oid
            o = Order()
            o.action = "BUY"
            o.orderType = "ZZZ"  # invalid -> reject
            o.totalQuantity = float(args.qty or 1.0)
            o.tif = "DAY"
            san = sanitize_order_v0(o)

            order_spec.update({"action": o.action, "type": o.orderType, "qty": o.totalQuantity, "sanitized": san})
            _write_json(evidence_dir / "order_spec.json", order_spec)

            app.placeOrder(oid, contract_obj, o)
            snap = app.wait_until(
                oid,
                timeout_s=max(6.0, float(args.listen_after_place_s)),
                predicate=lambda s: (s.get("errors") and len(_classify_order_errors(s["errors"])[1]) > 0),
            )
            warns, fails = _classify_order_errors(list(snap.get("errors") or []))
            if not fails:
                raise RuntimeError(f"REJECT_NOT_OBSERVED: last_snap={snap}")

            terminal_state = "REJECTED"
            out_order_ids = [oid]

        else:  # scenario_fill_v1
            if str(args.confirm_fill).strip().upper() != "YES":
                raise RuntimeError("FILL_CONFIRM_REQUIRED: pass --confirm-fill YES")

            pos = _load_positions_snapshot(repo)
            if not pos and str(args.confirm_roundtrip).strip().upper() != "YES":
                raise RuntimeError("POSITIONS_SNAPSHOT_MISSING: write args/data/ibkr_positions_snapshot_v0.json OR pass --confirm-roundtrip YES")

            sym = str(contract_item.get("symbol") or args.symbol or "")
            if not sym:
                raise RuntimeError("SYMBOL_REQUIRED_FOR_FILL")

            qty_pos = _extract_pos_qty(pos, sym) if pos else 0.0

            # If position exists -> EXIT ONLY (allowed under ONLY_EXITS/HALT)
            if abs(qty_pos) > 0.0:
                oid = base_oid
                o = Order()
                o.orderType = "MKT"
                o.tif = "DAY"
                o.totalQuantity = float(abs(qty_pos))
                o.action = "SELL" if qty_pos > 0 else "BUY"
                san = sanitize_order_v0(o)

                order_spec.update({"mode": "EXIT_ONLY", "action": o.action, "type": o.orderType, "qty": o.totalQuantity, "pos_qty": qty_pos, "sanitized": san})
                _write_json(evidence_dir / "order_spec.json", order_spec)

                app.placeOrder(oid, contract_obj, o)
                snap = app.wait_until(
                    oid,
                    timeout_s=float(args.fill_wait_s),
                    predicate=lambda s: _is_filled(s) or (s.get("errors") and len(_classify_order_errors(s["errors"])[1]) > 0),
                )
                warns, fails = _classify_order_errors(list(snap.get("errors") or []))
                if fails:
                    raise RuntimeError(f"FILL_FAILED_ERRORS: {fails[:1]}")
                if not _is_filled(snap):
                    raise RuntimeError(f"FILL_NOT_CONFIRMED: orderStatus={snap.get('orderStatus')} execDetails={len(snap.get('execDetails') or [])}")

                terminal_state = "FILLED"
                out_order_ids = [oid]

            else:
                # Position==0 -> Roundtrip requires HALT + explicit confirm-roundtrip
                if _u(cp.get("global_mode")) != "HALT":
                    raise RuntimeError(f"ROUNDTRIP_REQUIRES_HALT: global_mode={_u(cp.get('global_mode'))}")
                if str(args.confirm_roundtrip).strip().upper() != "YES":
                    raise RuntimeError("ROUNDTRIP_CONFIRM_REQUIRED: pass --confirm-roundtrip YES")

                q = float(args.roundtrip_qty or 1.0)
                if q <= 0:
                    raise RuntimeError("ROUNDTRIP_QTY_INVALID")

                legs: List[Dict[str, Any]] = []
                out_order_ids = []

                # LEG1 BUY
                oid1 = base_oid
                o1 = Order()
                o1.orderType = "MKT"
                o1.tif = "DAY"
                o1.totalQuantity = q
                o1.action = "BUY"
                san1 = sanitize_order_v0(o1)
                legs.append({"leg": "BUY", "orderId": oid1, "qty": q, "sanitized": san1})
                out_order_ids.append(oid1)

                # LEG2 SELL
                oid2 = base_oid + 1
                o2 = Order()
                o2.orderType = "MKT"
                o2.tif = "DAY"
                o2.totalQuantity = q
                o2.action = "SELL"
                san2 = sanitize_order_v0(o2)
                legs.append({"leg": "SELL", "orderId": oid2, "qty": q, "sanitized": san2})
                out_order_ids.append(oid2)

                order_spec.update({"mode": "ROUNDTRIP", "symbol": sym, "roundtrip_qty": q, "legs": legs})
                _write_json(evidence_dir / "order_spec.json", order_spec)

                app.placeOrder(oid1, contract_obj, o1)
                snap1 = app.wait_until(
                    oid1,
                    timeout_s=float(args.fill_wait_s),
                    predicate=lambda s: _is_filled(s) or (s.get("errors") and len(_classify_order_errors(s["errors"])[1]) > 0),
                )
                warns1, fails1 = _classify_order_errors(list(snap1.get("errors") or []))
                if fails1:
                    raise RuntimeError(f"ROUNDTRIP_BUY_FAILED: {fails1[:1]}")
                if not _is_filled(snap1):
                    raise RuntimeError("ROUNDTRIP_BUY_NOT_FILLED")

                time.sleep(0.3)

                app.placeOrder(oid2, contract_obj, o2)
                snap2 = app.wait_until(
                    oid2,
                    timeout_s=float(args.fill_wait_s),
                    predicate=lambda s: _is_filled(s) or (s.get("errors") and len(_classify_order_errors(s["errors"])[1]) > 0),
                )
                warns2, fails2 = _classify_order_errors(list(snap2.get("errors") or []))
                if fails2:
                    raise RuntimeError(f"ROUNDTRIP_SELL_FAILED: {fails2[:1]}")
                if not _is_filled(snap2):
                    raise RuntimeError("ROUNDTRIP_SELL_NOT_FILLED")

                terminal_state = "FILLED_ROUNDTRIP"

        out = {
            "schema": "terminal_scenario_v1",
            "ts_utc": _utc_now_iso(),
            "scenario": scenario,
            "run_id": run_id,
            "ok": True,
            "exit_code": 0,
            "terminal_state": terminal_state,
            "order_id": out_order_ids[0] if out_order_ids else None,
            "order_ids": out_order_ids,
            "conn": {"host": host, "port": port, "client_id": client_id},
            "evidence_dir": str(evidence_dir),
            "written": {
                "control_plane_snapshot": str(evidence_dir / "control_plane_snapshot.json"),
                "contract_item": str(evidence_dir / "contract_item.json"),
                "order_spec": str(evidence_dir / "order_spec.json"),
                "ib_events": str(events_jsonl),
            },
        }
        _write_json(evidence_dir / "terminal_result.json", out)
        _one_line(out)
        return 0

    except Exception as e:
        out = {
            "schema": "terminal_scenario_v1",
            "ts_utc": _utc_now_iso(),
            "scenario": scenario,
            "run_id": run_id,
            "ok": False,
            "exit_code": 1,
            "error": f"{type(e).__name__}: {e}",
            "evidence_dir": str(evidence_dir),
        }
        _write_json(evidence_dir / "terminal_result.json", out)
        _one_line(out)
        return 1

    finally:
        try:
            app.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())




