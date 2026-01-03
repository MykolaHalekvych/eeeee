# args/stage7/as_v1.py
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@dataclass
class FSMState:
    state: str = "WAIT"
    cooldown_until_utc: Optional[str] = None
    last_transition_utc: Optional[str] = None


def iso_parse(ts: str) -> datetime:
    # expects Z
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def in_cooldown(now: str, cooldown_until: Optional[str]) -> bool:
    if not cooldown_until:
        return False
    try:
        return iso_parse(now) < iso_parse(cooldown_until)
    except Exception:
        return False


def transition(now: str, fsm: FSMState, pos_qty: float, open_orders: int, intent_type: str, cooldown_sec: int) -> FSMState:
    s = fsm.state

    # Strong rule: if open orders exist, avoid new entry transitions
    if open_orders > 0 and s in ("WAIT", "ARMED"):
        s = "WAIT"

    if s == "WAIT":
        if pos_qty != 0:
            s = "IN_POSITION"
        elif intent_type == "ENTER":
            s = "ARMED"

    elif s == "ARMED":
        if pos_qty != 0:
            s = "IN_POSITION"
        elif intent_type != "ENTER":
            s = "WAIT"

    elif s == "IN_POSITION":
        if pos_qty == 0 and open_orders == 0:
            s = "COOL_DOWN"
        elif intent_type in ("EXIT", "REDUCE", "TP"):
            s = "EXITING"
        else:
            s = "MANAGE"

    elif s == "MANAGE":
        if pos_qty == 0 and open_orders == 0:
            s = "COOL_DOWN"
        elif intent_type in ("EXIT", "REDUCE", "TP"):
            s = "EXITING"

    elif s == "EXITING":
        if pos_qty == 0 and open_orders == 0:
            s = "COOL_DOWN"

    elif s == "COOL_DOWN":
        if not in_cooldown(now, fsm.cooldown_until_utc):
            s = "WAIT"

    # Apply cooldown start
    if fsm.state != s:
        fsm.last_transition_utc = now
        fsm.state = s
        if s == "COOL_DOWN":
            until = iso_parse(now) + timedelta(seconds=int(cooldown_sec))
            fsm.cooldown_until_utc = until.isoformat().replace("+00:00", "Z")
    return fsm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--universe", default="MHG")  # comma-separated
    ap.add_argument("--cooldown-sec", type=int, default=900)
    args = ap.parse_args()

    repo = Path(args.repo)
    now = utc_now_iso()

    universe = [x.strip() for x in args.universe.split(",") if x.strip()]
    if not universe:
        universe = ["MHG"]

    # Inputs
    soak_path = repo / "args" / "data" / "ops_soak_gate.json"
    reconcile_path = repo / "args" / "data" / "reconcile_evidence_latest.json"
    cursor_path = repo / "args" / "data" / "as_v1.cursor.json"
    latest_path = repo / "args" / "data" / "as_v1_latest.json"

    soak = load_json(soak_path) or {}
    reconcile = load_json(reconcile_path) or {}

    cursor = load_json(cursor_path) or {}
    fsm_map: Dict[str, Any] = cursor.get("fsm") or {}

    # Determine gate status
    soak_status = str(soak.get("status") or "UNKNOWN").upper()
    reconcile_status = str(reconcile.get("status") or "UNKNOWN").upper()
    gates_ok = (soak_status == "OK") and (reconcile_status == "OK")

    warns: List[str] = []
    if soak_status != "OK":
        warns.append(f"soak_gate_not_ok:{soak_status}")
    if reconcile_status != "OK":
        warns.append(f"reconcile_not_ok:{reconcile_status}")

    # Extract positions/orders from reconcile (best-effort)
    positions = reconcile.get("positions_snapshot") or reconcile.get("positions") or {}
    open_orders = reconcile.get("open_orders_snapshot") or reconcile.get("open_orders") or []

    def get_pos_qty(sym: str) -> float:
        # accept dict mapping or list rows
        if isinstance(positions, dict):
            v = positions.get(sym)
            if isinstance(v, (int, float)):
                return float(v)
        if isinstance(positions, list):
            for row in positions:
                if isinstance(row, dict) and str(row.get("symbol") or row.get("localSymbol") or "") == sym:
                    try:
                        return float(row.get("position") or 0.0)
                    except Exception:
                        return 0.0
        return 0.0

    def count_open_orders(sym: str) -> int:
        if isinstance(open_orders, list):
            c = 0
            for row in open_orders:
                if isinstance(row, dict):
                    s = str(row.get("symbol") or row.get("localSymbol") or "")
                    if s == sym:
                        c += 1
            return c
        return 0

    # Signals placeholder: until AS features exist, always NONE
    intents: List[Dict[str, Any]] = []
    for sym in universe:
        pos_qty = get_pos_qty(sym)
        oo = count_open_orders(sym)

        # Minimal logic: do nothing unless gates OK (and later signals)
        intent_type = "NONE"
        confidence = 0.0
        reason = ["no_signals_v1"]

        # FSM update
        fsm_raw = fsm_map.get(sym) or {}
        fsm = FSMState(
            state=str(fsm_raw.get("state") or "WAIT"),
            cooldown_until_utc=fsm_raw.get("cooldown_until_utc"),
            last_transition_utc=fsm_raw.get("last_transition_utc"),
        )
        fsm = transition(now, fsm, pos_qty, oo, intent_type, int(args.cooldown_sec))

        intents.append(
            {
                "intent_id": f"ASV1:{now}:{sym}:{intent_type}",
                "ts_utc": now,
                "instrument": sym,
                "type": intent_type,
                "side": None,
                "qty": 0,
                "confidence": confidence,
                "reason": reason,
                "actionable": False,
                "blocked_by": ["execution_mode_dryrun", "no_signals_v1"] if gates_ok else ["gates_not_ok"],
            }
        )

        fsm_map[sym] = {
            "state": fsm.state,
            "cooldown_until_utc": fsm.cooldown_until_utc,
            "last_transition_utc": fsm.last_transition_utc,
            "pos_qty": pos_qty,
            "open_orders": oo,
        }

    status = "OK" if gates_ok else "WARN"
    exit_code = 0 if gates_ok else 1

    out = {
        "schema": "as_v1_latest",
        "ts_utc": now,
        "status": status,
        "exit_code": exit_code,
        "inputs": {
            "ops_soak_gate": str(soak_path),
            "reconcile_latest": str(reconcile_path),
        },
        "gates": {
            "soak_status": soak_status,
            "reconcile_status": reconcile_status,
            "gates_ok": gates_ok,
        },
        "warns": warns,
        "fsm": fsm_map,
        "intents": intents,
    }

    save_json(latest_path, out)

    # Evidence dir
    ev_dir = repo / "args" / "ops_evidence" / "as_v1" / now.replace(":", "").replace("-", "").replace(".", "")
    save_json(ev_dir / "as_v1_latest.json", out)

    cursor_out = {
        "schema": "as_v1_cursor",
        "last_run_utc": now,
        "fsm": fsm_map,
    }
    save_json(cursor_path, cursor_out)

    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
