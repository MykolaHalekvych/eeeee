# args/wa/wa_ibkr_executor_v0.py
from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from args.wa.order_ledger_v0 import OrderLedgerV0, compute_idempotency_key_from_plan

SCHEMA_VERSION = "wa_ibkr_executor_v0"

# Supported actionable plans (from orders_sendplan_*.jsonl)
ACTIONABLE_PLAN_KINDS = {
    "PLAN_IBKR_PLACE_ORDER",
    "IBKR_PLACE_ORDER",
    "PLACE_ORDER_IBKR",
}


def _b01(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in {"1", "true", "yes", "y", "on"}


def _json_compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))


def _write_jsonl_line(f, obj: Dict[str, Any]) -> None:
    f.write(_json_compact(obj))
    f.write("\n")


def _iter_jsonl_dicts(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _norm(s: Any) -> str:
    if not isinstance(s, str):
        return ""
    return s.strip().upper()


def _repo_root() -> Path:
    # .../args/wa/wa_ibkr_executor_v0.py -> parents[2] == repo root
    return Path(__file__).resolve().parents[2]


def _default_contract_path(repo_root: Path, instrument: str) -> Optional[Path]:
    inst = _norm(instrument)
    data_dir = repo_root / "args" / "data"
    if inst == "HG":
        p = data_dir / "ibkr_hg_contract_v1.json"
        return p if p.exists() else None
    return None


def _extract_contract_dict(plan: Dict[str, Any], repo_root: Path) -> Optional[Dict[str, Any]]:
    for k in ("contract", "ibkr_contract"):
        v = plan.get(k)
        if isinstance(v, dict) and v:
            return v

    cp = plan.get("contract_path")
    if isinstance(cp, str) and cp.strip():
        p = Path(cp.strip())
        if not p.is_absolute():
            p = repo_root / p
        if p.exists():
            obj = _read_json(p)
            return obj if isinstance(obj, dict) else None

    inst = str(plan.get("instrument") or "")
    dp = _default_contract_path(repo_root, inst)
    if dp and dp.exists():
        obj = _read_json(dp)
        return obj if isinstance(obj, dict) else None

    return None


def _extract_order_dict(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for k in ("order", "ibkr_order"):
        v = plan.get(k)
        if isinstance(v, dict) and v:
            return v

    payload = plan.get("payload")
    if isinstance(payload, dict):
        for k in ("order", "ibkr_order"):
            v = payload.get(k)
            if isinstance(v, dict) and v:
                return v

    return None


def _safe_setattr(obj: Any, k: str, v: Any) -> None:
    if not isinstance(k, str) or not k:
        return
    try:
        setattr(obj, k, v)
    except Exception:
        pass


def _dict_to_ib_contract(contract_dict: Dict[str, Any]):
    from ibapi.contract import Contract  # type: ignore

    c = Contract()
    for k, v in contract_dict.items():
        _safe_setattr(c, k, v)
    return c


def _dict_to_ib_order(order_dict: Dict[str, Any]):
    from ibapi.order import Order  # type: ignore

    o = Order()
    for k, v in order_dict.items():
        _safe_setattr(o, k, v)
    return o


@dataclass(frozen=True)
class IbkrConn:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 11
    timeout_s: float = 15.0


class _IBSimpleApp:
    def __init__(self, conn: IbkrConn):
        from ibapi.client import EClient  # type: ignore
        from ibapi.wrapper import EWrapper  # type: ignore

        class App(EWrapper, EClient):  # type: ignore
            def __init__(self, outer: "_IBSimpleApp"):
                EClient.__init__(self, self)
                self._outer = outer

            def nextValidId(self, orderId: int) -> None:
                self._outer._on_next_valid_id(orderId)

            def openOrder(self, orderId, contract, order, orderState) -> None:  # noqa: N802
                self._outer._on_open_order(orderId, contract, order, orderState)

            def orderStatus(  # noqa: N802
                self,
                orderId,
                status,
                filled,
                remaining,
                avgFillPrice,
                permId,
                parentId,
                lastFillPrice,
                clientId,
                whyHeld,
                mktCapPrice,
            ) -> None:
                self._outer._on_order_status(
                    orderId=orderId,
                    status=status,
                    filled=filled,
                    remaining=remaining,
                    avgFillPrice=avgFillPrice,
                    lastFillPrice=lastFillPrice,
                    whyHeld=whyHeld,
                )

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson="") -> None:  # type: ignore  # noqa: N802
                self._outer._on_error(reqId, errorCode, errorString, advancedOrderRejectJson)

        self._conn = conn
        self._app = App(self)

        self._thread: Optional[threading.Thread] = None
        self._next_id: Optional[int] = None
        self._next_id_ev = threading.Event()

        self._lock = threading.Lock()
        self._ack: Dict[int, Dict[str, Any]] = {}
        self._reject: Dict[int, Dict[str, Any]] = {}
        self._status: Dict[int, Dict[str, Any]] = {}

    def connect(self) -> None:
        self._app.connect(self._conn.host, int(self._conn.port), int(self._conn.client_id))
        self._thread = threading.Thread(target=self._app.run, daemon=True)
        self._thread.start()
        if not self._next_id_ev.wait(timeout=self._conn.timeout_s):
            raise RuntimeError("IBKR connect: timeout waiting for nextValidId()")

    def disconnect(self) -> None:
        try:
            self._app.disconnect()
        except Exception:
            pass

    def _on_next_valid_id(self, order_id: int) -> None:
        with self._lock:
            self._next_id = int(order_id)
        self._next_id_ev.set()

    def _on_open_order(self, order_id: int, contract: Any, order: Any, order_state: Any) -> None:
        try:
            st = getattr(order_state, "status", None)
        except Exception:
            st = None
        with self._lock:
            self._ack[int(order_id)] = {
                "event": "openOrder",
                "order_id": int(order_id),
                "order_state_status": st,
            }

    def _on_order_status(
        self,
        *,
        orderId: int,
        status: Any,
        filled: Any,
        remaining: Any,
        avgFillPrice: Any,
        lastFillPrice: Any,
        whyHeld: Any,
    ) -> None:
        with self._lock:
            self._status[int(orderId)] = {
                "event": "orderStatus",
                "order_id": int(orderId),
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "avgFillPrice": avgFillPrice,
                "lastFillPrice": lastFillPrice,
                "whyHeld": whyHeld,
            }

    def _on_error(self, req_id: Any, error_code: Any, error_str: Any, advanced: Any) -> None:
        try:
            oid = int(req_id)
        except Exception:
            oid = -1
        payload = {
            "event": "error",
            "req_id": req_id,
            "error_code": error_code,
            "error_string": str(error_str),
        }
        if isinstance(advanced, str) and advanced.strip():
            payload["advanced_order_reject_json"] = advanced
        with self._lock:
            self._reject[oid] = payload

    def next_order_id(self) -> int:
        if not self._next_id_ev.is_set():
            if not self._next_id_ev.wait(timeout=self._conn.timeout_s):
                raise RuntimeError("IBKR: no nextValidId available")
        with self._lock:
            assert self._next_id is not None
            oid = self._next_id
            self._next_id += 1
        return int(oid)

    def place_order(self, *, order_id: int, contract_obj: Any, order_obj: Any) -> Tuple[bool, Dict[str, Any]]:
        with self._lock:
            self._ack.pop(int(order_id), None)
            self._reject.pop(int(order_id), None)
            self._status.pop(int(order_id), None)

        self._app.placeOrder(int(order_id), contract_obj, order_obj)

        t0 = time.time()
        while (time.time() - t0) < self._conn.timeout_s:
            with self._lock:
                if int(order_id) in self._reject:
                    return False, dict(self._reject[int(order_id)])
                if int(order_id) in self._ack:
                    out = dict(self._ack[int(order_id)])
                    st = self._status.get(int(order_id))
                    if isinstance(st, dict):
                        out["order_status"] = dict(st)
                    return True, out
            time.sleep(0.05)

        with self._lock:
            st = self._status.get(int(order_id))
        if isinstance(st, dict):
            return True, {"event": "timeout_but_status_seen", "order_status": dict(st)}
        return False, {"event": "timeout", "error_string": "Timeout waiting for openOrder/error callback"}


def _try_submit_via_sender_real(
    *,
    contract_dict: Dict[str, Any],
    order_dict: Dict[str, Any],
    conn: IbkrConn,
) -> Optional[Tuple[bool, Dict[str, Any]]]:
    try:
        import args.ibkr.ibkr_sender_real_v1 as sender  # type: ignore
    except Exception:
        return None

    candidates = ["submit_order_v0", "submit_order", "send_order", "place_order", "execute_order"]

    def _coerce_result(res: Any) -> Optional[Tuple[bool, Dict[str, Any]]]:
        if isinstance(res, tuple) and len(res) == 2:
            ok = bool(res[0])
            detail = res[1] if isinstance(res[1], dict) else {"detail": res[1]}
            return ok, detail
        if isinstance(res, dict):
            ok = bool(res.get("ok", True))
            return ok, res
        if isinstance(res, bool):
            return res, {"ok": res}
        return None

    for name in candidates:
        fn = getattr(sender, name, None)
        if not callable(fn):
            continue

        attempts = [
            lambda: fn(contract_dict=contract_dict, order_dict=order_dict, host=conn.host, port=conn.port, client_id=conn.client_id, timeout_s=conn.timeout_s),
            lambda: fn(contract=contract_dict, order=order_dict, host=conn.host, port=conn.port, client_id=conn.client_id, timeout_s=conn.timeout_s),
            lambda: fn(contract_dict, order_dict, conn.host, conn.port, conn.client_id),
            lambda: fn(contract_dict, order_dict),
        ]
        for a in attempts:
            try:
                res = a()
                out = _coerce_result(res)
                if out is not None:
                    return out
            except TypeError:
                continue
            except Exception as e:
                return False, {"event": "sender_real_exception", "error": repr(e), "callable": name}

    return None


def _is_actionable_plan(plan: Dict[str, Any]) -> bool:
    pk = _norm(plan.get("plan_kind"))
    if pk in ACTIONABLE_PLAN_KINDS:
        return True
    if pk.startswith("PLAN_IBKR_"):
        return True
    return False


def _build_event_base(plan: Dict[str, Any], *, exec_id: str) -> Dict[str, Any]:
    return {
        "run_id": plan.get("run_id"),
        "index": plan.get("index"),
        "ts": plan.get("ts"),
        "instrument": plan.get("instrument"),
        "timeframe": plan.get("timeframe"),
        "env": plan.get("env"),
        "exec_id": exec_id,
        "source": "IBKR_EXECUTOR_V0",
        "plan_id": plan.get("plan_id"),
        "plan_kind": plan.get("plan_kind"),
        "payload_id": plan.get("payload_id"),
        "payload_kind": plan.get("payload_kind"),
        "execute": bool(plan.get("execute", False)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="wa_ibkr_executor_v0")

    # Input selection
    ap.add_argument("--sendplan", default="", help="Path to orders_sendplan_<run_id>.jsonl (optional if --run-id is given)")
    ap.add_argument("--run-id", default="", help="If set, will use args/data/orders_sendplan_<run_id>.jsonl")

    # Safety
    ap.add_argument("--execute", default="0", help="0/1. When 1, executor will attempt IBKR placeOrder for actionable plans.")
    ap.add_argument("--max-orders", type=int, default=0, help="Safety limit: 0 means no limit")

    # Ledger (Stage 5C)
    ap.add_argument("--ledger-path", default="args/data/order_ledger_v0.jsonl")
    ap.add_argument("--ledger-enabled", default="1", help="0/1. Enabled only when execute=1")
    ap.add_argument("--ledger-allow-retry", default="0", help="0/1. SAFE default 0 (no retry after REJECT/ERROR)")

    # Output
    ap.add_argument("--out", default="", help="Optional output JSONL for exec events. Default: args/data/orders_exec_events_<run_id>.jsonl")

    # Connection
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=11)
    ap.add_argument("--timeout-s", type=float, default=15.0)

    args = ap.parse_args()
    repo_root = _repo_root()

    # Resolve sendplan path
    run_id_arg = str(args.run_id or "").strip()
    sendplan_arg = str(args.sendplan or "").strip()

    if not sendplan_arg and not run_id_arg:
        raise SystemExit("Need --sendplan <path> or --run-id <RUN_ID>")

    if not sendplan_arg and run_id_arg:
        sendplan_path = repo_root / "args" / "data" / f"orders_sendplan_{run_id_arg}.jsonl"
    else:
        sendplan_path = Path(sendplan_arg)
        if not sendplan_path.is_absolute():
            sendplan_path = repo_root / sendplan_path

    if not sendplan_path.exists():
        raise FileNotFoundError(f"sendplan not found: {sendplan_path}")

    execute = _b01(args.execute)
    conn = IbkrConn(
        host=str(args.host),
        port=int(args.port),
        client_id=int(args.client_id),
        timeout_s=float(args.timeout_s),
    )

    exec_id = uuid.uuid4().hex[:12]

    plans_seen = 0
    plans_actionable = 0
    orders_attempted = 0
    orders_skipped = 0
    orders_skipped_duplicate = 0
    acks = 0
    rejects = 0
    parse_errors = 0

    # Determine run_id from first plan (best-effort)
    first_run_id: Optional[str] = run_id_arg or None
    if first_run_id is None:
        for plan in _iter_jsonl_dicts(sendplan_path):
            rid = plan.get("run_id")
            if isinstance(rid, str) and rid.strip():
                first_run_id = rid.strip()
                break

    # Resolve output path
    if str(args.out or "").strip():
        out_path = Path(str(args.out).strip())
        if not out_path.is_absolute():
            out_path = repo_root / out_path
    else:
        data_dir = repo_root / "args" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        suffix = first_run_id or "unknown"
        out_path = data_dir / f"orders_exec_events_{suffix}.jsonl"

    # Ledger init (only when execute=1)
    ledger: Optional[OrderLedgerV0] = None
    ledger_enabled = _b01(args.ledger_enabled)
    ledger_allow_retry = _b01(args.ledger_allow_retry)
    if execute and ledger_enabled:
        ledger_path = Path(str(args.ledger_path))
        if not ledger_path.is_absolute():
            ledger_path = repo_root / ledger_path
        ledger = OrderLedgerV0(ledger_path)

    # IB connection (lazy)
    ib_app: Optional[_IBSimpleApp] = None
    ib_connected = False

    def _ensure_connected() -> None:
        nonlocal ib_app, ib_connected
        if ib_connected:
            return
        ib_app = _IBSimpleApp(conn)
        ib_app.connect()
        ib_connected = True

    with out_path.open("w", encoding="utf-8") as fout:
        for raw_line in sendplan_path.open("r", encoding="utf-8-sig", errors="replace"):
            s = raw_line.strip()
            if not s:
                continue
            try:
                plan = json.loads(s)
            except Exception:
                parse_errors += 1
                continue
            if not isinstance(plan, dict):
                continue

            plans_seen += 1
            if first_run_id is None:
                rid = plan.get("run_id")
                if isinstance(rid, str) and rid.strip():
                    first_run_id = rid.strip()

            if not _is_actionable_plan(plan):
                continue
            plans_actionable += 1

            # Plan-level execute gate
            plan_execute = bool(plan.get("execute", False))
            if (not execute) or (not plan_execute):
                orders_skipped += 1
                continue

            if args.max_orders and int(args.max_orders) > 0 and orders_attempted >= int(args.max_orders):
                orders_skipped += 1
                continue

            contract_dict = _extract_contract_dict(plan, repo_root)
            order_dict = _extract_order_dict(plan)

            if not isinstance(contract_dict, dict) or not contract_dict:
                evt = _build_event_base(plan, exec_id=exec_id)
                evt.update(
                    {
                        "kind": "ORDER_REJECT",
                        "reason": "missing_contract",
                        "details": {"error": "No contract info in plan and no default contract mapping found."},
                    }
                )
                _write_jsonl_line(fout, evt)
                rejects += 1
                continue

            if not isinstance(order_dict, dict) or not order_dict:
                evt = _build_event_base(plan, exec_id=exec_id)
                evt.update(
                    {
                        "kind": "ORDER_REJECT",
                        "reason": "missing_order",
                        "details": {"error": "No order info in plan (expected plan.order/plan.ibkr_order/payload.*)"},
                    }
                )
                _write_jsonl_line(fout, evt)
                rejects += 1
                continue

            # Ledger dedupe: reserve BEFORE submit
            ledger_key = ""
            plan_fp = ""
            if ledger is not None:
                ledger_key, plan_fp = compute_idempotency_key_from_plan(plan, prefix="ibkr_place_order")
                reserved = ledger.reserve(
                    ledger_key,
                    run_id=str(first_run_id or plan.get("run_id") or "unknown"),
                    plan_meta={
                        "plan_kind": plan.get("plan_kind"),
                        "plan_fp": plan_fp,
                        "plan_id": plan.get("plan_id"),
                        "payload_id": plan.get("payload_id"),
                        "index": plan.get("index"),
                    },
                    allow_retry=ledger_allow_retry,
                )
                if not reserved:
                    evt = _build_event_base(plan, exec_id=exec_id)
                    evt.update(
                        {
                            "kind": "ORDER_SKIP_DUPLICATE",
                            "ledger_key": ledger_key,
                            "details": {"plan_fp": plan_fp},
                        }
                    )
                    _write_jsonl_line(fout, evt)
                    orders_skipped_duplicate += 1
                    continue

            orders_attempted += 1

            # 1) Try sender_real_v1
            sender_res = _try_submit_via_sender_real(contract_dict=contract_dict, order_dict=order_dict, conn=conn)
            if sender_res is not None:
                ok, detail = sender_res
                evt = _build_event_base(plan, exec_id=exec_id)
                evt["ibkr"] = {"via": "ibkr_sender_real_v1"}
                if ledger_key:
                    evt["ledger_key"] = ledger_key

                if ok:
                    evt["kind"] = "ORDER_ACK"
                    evt["details"] = detail
                    acks += 1
                    if ledger is not None:
                        ledger.finalize(ledger_key, run_id=str(first_run_id or "unknown"), status="ACK", result={"ack": detail})
                else:
                    evt["kind"] = "ORDER_REJECT"
                    evt["details"] = detail
                    rejects += 1
                    if ledger is not None:
                        ledger.finalize(ledger_key, run_id=str(first_run_id or "unknown"), status="REJECT", result={"reject": detail})

                _write_jsonl_line(fout, evt)
                continue

            # 2) Fallback: direct ibapi
            try:
                _ensure_connected()
                assert ib_app is not None

                plan_order_id = plan.get("ibkr_order_id") or plan.get("order_id")
                if isinstance(plan_order_id, int):
                    order_id = int(plan_order_id)
                else:
                    order_id = ib_app.next_order_id()

                contract_obj = _dict_to_ib_contract(contract_dict)
                order_obj = _dict_to_ib_order(order_dict)

                ok, detail = ib_app.place_order(order_id=order_id, contract_obj=contract_obj, order_obj=order_obj)

                evt = _build_event_base(plan, exec_id=exec_id)
                evt["ibkr"] = {"via": "ibapi_direct", "order_id": int(order_id)}
                if ledger_key:
                    evt["ledger_key"] = ledger_key

                if ok:
                    evt["kind"] = "ORDER_ACK"
                    evt["details"] = detail
                    acks += 1
                    if ledger is not None:
                        ledger.finalize(ledger_key, run_id=str(first_run_id or "unknown"), status="ACK", result={"ack": detail})
                else:
                    evt["kind"] = "ORDER_REJECT"
                    evt["details"] = detail
                    rejects += 1
                    if ledger is not None:
                        ledger.finalize(ledger_key, run_id=str(first_run_id or "unknown"), status="REJECT", result={"reject": detail})

                _write_jsonl_line(fout, evt)

            except Exception as e:
                evt = _build_event_base(plan, exec_id=exec_id)
                if ledger_key:
                    evt["ledger_key"] = ledger_key
                evt.update(
                    {
                        "kind": "ORDER_REJECT",
                        "reason": "executor_exception",
                        "details": {"error": repr(e)},
                    }
                )
                _write_jsonl_line(fout, evt)
                rejects += 1
                if ledger is not None and ledger_key:
                    ledger.finalize(
                        ledger_key,
                        run_id=str(first_run_id or "unknown"),
                        status="ERROR",
                        result={"error": {"type": type(e).__name__, "msg": str(e)}},
                    )

    if ib_app is not None:
        try:
            ib_app.disconnect()
        except Exception:
            pass

    summary = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "execute": execute,
        "sendplan_path": str(sendplan_path),
        "out_path": str(out_path),
        "ledger": {
            "enabled": bool(ledger is not None),
            "path": str((repo_root / Path(str(args.ledger_path))).resolve()) if (execute and ledger_enabled) else "",
            "allow_retry": ledger_allow_retry,
        },
        "conn": {"host": conn.host, "port": conn.port, "client_id": conn.client_id, "timeout_s": conn.timeout_s},
        "plans_seen": plans_seen,
        "plans_actionable": plans_actionable,
        "orders_attempted": orders_attempted,
        "orders_skipped": orders_skipped,
        "orders_skipped_duplicate": orders_skipped_duplicate,
        "acks": acks,
        "rejects": rejects,
        "parse_errors": parse_errors,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
