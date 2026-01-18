# args/stage7/as_v1.py
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# -------------------------
# Utils
# -------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def first_existing(paths: List[Path]) -> Path:
    for p in paths:
        if p.exists():
            return p
    return paths[0]


def _safe_read_text(path: Path) -> Tuple[Optional[str], Optional[str]]:
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        raw = raw.rstrip(b"\x00 \t\r\n")
        txt = raw.decode("utf-8", errors="replace").strip()
        return txt, None
    except Exception as e:
        return None, f"read_failed:{e}"


def load_json_retry(
    path: Path, tries: int = 6, sleep_sec: float = 0.05
) -> Tuple[Optional[Any], Optional[str]]:
    if not path.exists():
        return None, "missing"

    last_err = None
    for _ in range(max(1, tries)):
        txt, err = _safe_read_text(path)
        if txt is None:
            last_err = err
            time.sleep(sleep_sec)
            continue

        if txt and txt[0] not in "{[":
            last_err = "not_json_prefix"
            time.sleep(sleep_sec)
            continue
        if txt and txt[-1] not in "}]":
            last_err = "possible_truncation"
            time.sleep(sleep_sec)
            continue

        try:
            return json.loads(txt), None
        except Exception as e:
            last_err = f"json_parse_failed:{e}"
            time.sleep(sleep_sec)

    return None, last_err or "unknown_parse_error"


def find_status_carrier(obj: Any, max_depth: int = 5) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    queue: List[Tuple[Dict[str, Any], int]] = [(obj, 0)]
    seen = set()
    while queue:
        d, depth = queue.pop(0)
        if id(d) in seen:
            continue
        seen.add(id(d))

        keys = {str(k).lower() for k in d.keys()}
        if any(
            k in keys
            for k in (
                "status",
                "ok",
                "exit_code",
                "mode",
                "risk_envelope",
                "envelope",
                "execution_mode",
            )
        ):
            return d

        if depth >= max_depth:
            continue

        for v in d.values():
            if isinstance(v, dict):
                queue.append((v, depth + 1))
    return None


def ok_status_from_any(obj: Any) -> str:
    if obj is None:
        return "UNKNOWN"
    carrier = find_status_carrier(obj) or (obj if isinstance(obj, dict) else None)
    if not isinstance(carrier, dict):
        return "UNKNOWN"

    s = str(carrier.get("status") or "").upper().strip()
    if s:
        return s

    ok = carrier.get("ok")
    exit_code = carrier.get("exit_code")
    try:
        ec = int(exit_code) if exit_code is not None else None
    except Exception:
        ec = None

    if ok is True and (ec is None or ec == 0):
        return "OK"
    if ok is False:
        return "FAIL"
    if ec is not None:
        if ec >= 2:
            return "FAIL"
        if ec == 1:
            return "WARN"
    return "UNKNOWN"


def _nested_get(d: Any, path: List[str]) -> Any:
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _as_path(repo: Path, p: Any) -> Optional[Path]:
    if not isinstance(p, str) or not p:
        return None
    try:
        pp = Path(p)
        if pp.is_absolute():
            return pp
        return (repo / pp).resolve()
    except Exception:
        return None


def _base_symbol_from_local(local_symbol: str) -> str:
    # IBKR localSymbol часто: "MHG MAR26" или "MHG   MAR26" — берём первый токен
    s = (local_symbol or "").strip()
    if not s:
        return ""
    return s.split()[0].strip()


def _symbol_match(sym: str, symbol: str, local_symbol: str) -> bool:
    sym = (sym or "").strip()
    if not sym:
        return False
    symbol = (symbol or "").strip()
    local_symbol = (local_symbol or "").strip()
    token = _base_symbol_from_local(local_symbol)
    return (symbol == sym) or (local_symbol == sym) or (token == sym)


# -------------------------
# Control plane
# -------------------------


def get_control_plane_mode(cp: Any) -> Tuple[str, List[str]]:
    warns: List[str] = []
    if not isinstance(cp, dict):
        return "UNKNOWN", ["control_plane_missing_or_invalid"]

    def norm(x: Any) -> str:
        return str(x).upper().strip() if isinstance(x, str) else ""

    global_mode = norm(cp.get("global_mode"))
    risk_mode = ""
    env = cp.get("risk_envelope") or {}
    if isinstance(env, dict):
        risk_mode = norm(env.get("mode"))

    allowed = {"ALLOW_NEW_ENTRIES", "ONLY_EXITS", "NO_TRADE"}

    chosen = "UNKNOWN"
    if global_mode in allowed:
        chosen = global_mode
    elif risk_mode in allowed:
        chosen = risk_mode

    if global_mode in allowed and risk_mode in allowed and global_mode != risk_mode:
        warns.append(
            f"control_plane_mode_conflict:global_mode={global_mode}:risk_envelope.mode={risk_mode}"
        )

    if chosen == "UNKNOWN":
        warns.append("mode_unknown")

    return chosen, warns


def get_control_plane_execution_mode(cp: Any) -> str:
    if not isinstance(cp, dict):
        return "UNKNOWN"
    v = cp.get("execution_mode")
    if isinstance(v, str) and v:
        return v.upper()
    if cp.get("enable_paper_execution") is True:
        return "PAPER_EXECUTION_ENABLED"
    return "UNKNOWN"


# -------------------------
# FSM
# -------------------------


@dataclass
class FSMState:
    state: str = "WAIT"
    cooldown_until_utc: Optional[str] = None
    last_transition_utc: Optional[str] = None


def in_cooldown(now_utc: str, cooldown_until_utc: Optional[str]) -> bool:
    if not cooldown_until_utc:
        return False
    try:
        return iso_parse(now_utc) < iso_parse(cooldown_until_utc)
    except Exception:
        return False


def transition(
    now_utc: str,
    fsm: FSMState,
    pos_qty: float,
    open_orders: int,
    intent_type: str,
    cooldown_sec: int,
) -> FSMState:
    s = fsm.state

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
        if not in_cooldown(now_utc, fsm.cooldown_until_utc):
            s = "WAIT"

    if fsm.state != s:
        fsm.last_transition_utc = now_utc
        fsm.state = s
        if s == "COOL_DOWN":
            until = iso_parse(now_utc) + timedelta(seconds=int(cooldown_sec))
            fsm.cooldown_until_utc = until.isoformat().replace("+00:00", "Z")
    return fsm


# -------------------------
# Reconcile: resolve snapshot paths from report (v1/v2)
# -------------------------


def resolve_positions_snapshot_path(repo: Path, reconcile_obj: Any) -> Optional[Path]:
    if not isinstance(reconcile_obj, dict):
        return None
    p = _nested_get(reconcile_obj, ["positions", "out_path"]) or _nested_get(
        reconcile_obj, ["positions", "outPath"]
    )
    return _as_path(repo, p)


def resolve_open_orders_snapshot_path(repo: Path, reconcile_obj: Any) -> Optional[Path]:
    if not isinstance(reconcile_obj, dict):
        return None
    p = _nested_get(reconcile_obj, ["open_orders", "jsonl_path"]) or _nested_get(
        reconcile_obj, ["open_orders", "jsonlPath"]
    )
    return _as_path(repo, p)


def load_positions_qty_from_snapshot(pos_snapshot: Any, sym: str) -> float:
    """
    Match position rows by:
      - row.symbol == sym
      - row.localSymbol == sym
      - base token of row.localSymbol (e.g. "MHG MAR26" -> "MHG") == sym
    """
    if not isinstance(pos_snapshot, dict):
        return 0.0
    rows = pos_snapshot.get("rows")
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue

            s_symbol = str(r.get("symbol") or "").strip()
            s_local = str(r.get("localSymbol") or "").strip()

            if _symbol_match(sym, s_symbol, s_local):
                try:
                    return float(r.get("position") or 0.0)
                except Exception:
                    return 0.0
    return 0.0


def count_open_orders_from_jsonl(path: Path, sym: str, max_lines: int = 5000) -> int:
    """
    Match open orders lines by:
      - obj.symbol == sym OR obj.ticker == sym
      - obj.localSymbol == sym
      - base token of obj.localSymbol == sym
    """
    if not path.exists():
        return 0
    c = 0
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        n += 1
        if n > max_lines:
            break
        t = line.strip()
        if not t.startswith("{"):
            continue
        try:
            obj = json.loads(t)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue

        s_symbol = str(obj.get("symbol") or obj.get("ticker") or "").strip()
        s_local = str(obj.get("localSymbol") or "").strip()

        if _symbol_match(sym, s_symbol, s_local):
            c += 1
    return c


# -------------------------
# Signals
# -------------------------


def load_signals(signals_obj: Any) -> Dict[str, Any]:
    if not isinstance(signals_obj, dict):
        return {}
    sigs = signals_obj.get("signals")
    return sigs if isinstance(sigs, dict) else {}


def policy_from_mode(mode: str) -> Tuple[bool, bool, List[str]]:
    warns: List[str] = []
    if mode == "ALLOW_NEW_ENTRIES":
        return True, True, warns
    if mode == "ONLY_EXITS":
        return False, True, warns
    if mode == "NO_TRADE":
        return False, False, warns
    warns.append("mode_unknown")
    return False, False, warns


def pick_intent(
    sym: str,
    pos_qty: float,
    open_orders: int,
    allow_entry: bool,
    allow_exit: bool,
    gate_ok: bool,
    gate_tag: str,
    sig: Dict[str, Any],
) -> Tuple[str, Optional[str], int, float, List[str], List[str]]:
    blocked_by: List[str] = []
    reason: List[str] = []

    # Hard blocks
    if open_orders > 0:
        blocked_by.append("open_orders_present")
        return "NONE", None, 0, 0.0, ["open_orders_present"], blocked_by

    if not sig:
        blocked_by.append("signals_missing")
        return "NONE", None, 0, 0.0, ["signals_missing"], blocked_by

    enter = bool(sig.get("enter"))
    exit_ = bool(sig.get("exit"))
    reduce = bool(sig.get("reduce"))
    tp = bool(sig.get("tp"))
    confidence = float(sig.get("confidence") or 0.0)
    qty = int(sig.get("qty") or 1)
    side = str(sig.get("side") or "BUY").upper()
    if side not in ("BUY", "SELL"):
        side = "BUY"

    # In position: exits dominate
    if pos_qty != 0:
        if not allow_exit:
            blocked_by.append("mode_blocks_exit")
            return "NONE", None, 0, confidence, ["mode_blocks_exit"], blocked_by

        # exit-on-warn: if gate is not OK, do not emit exit-like intents
        if not gate_ok:
            blocked_by.append(f"{gate_tag}_not_ok")

        if tp:
            reason.append("tp_signal")
            if not gate_ok:
                return "NONE", None, 0, confidence, reason, blocked_by
            return (
                "TP",
                ("SELL" if pos_qty > 0 else "BUY"),
                max(1, min(qty, abs(int(pos_qty)))),
                confidence,
                reason,
                blocked_by,
            )

        if reduce:
            reason.append("reduce_signal")
            if not gate_ok:
                return "NONE", None, 0, confidence, reason, blocked_by
            return (
                "REDUCE",
                ("SELL" if pos_qty > 0 else "BUY"),
                max(1, min(qty, abs(int(pos_qty)))),
                confidence,
                reason,
                blocked_by,
            )

        if exit_:
            reason.append("exit_signal")
            if not gate_ok:
                return "NONE", None, 0, confidence, reason, blocked_by
            return (
                "EXIT",
                ("SELL" if pos_qty > 0 else "BUY"),
                max(1, abs(int(pos_qty))),
                confidence,
                reason,
                blocked_by,
            )

        return "NONE", None, 0, confidence, ["in_position_no_exit_signal"], blocked_by

    # Flat: entry
    if enter:
        reason.append("enter_signal")
        if not allow_entry:
            blocked_by.append("mode_blocks_entry")
            return "NONE", None, 0, confidence, reason, blocked_by

        # entry gate: ENTER only when gate_ok (soak OK and reconcile OK)
        if not gate_ok:
            blocked_by.append(f"{gate_tag}_not_ok")
            return "NONE", None, 0, confidence, reason, blocked_by

        return "ENTER", side, qty, confidence, reason, blocked_by

    return "NONE", None, 0, confidence, ["no_entry_signal"], blocked_by


# -------------------------
# Main
# -------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--universe", default="MHG")  # comma-separated
    ap.add_argument("--cooldown-sec", type=int, default=900)
    args = ap.parse_args()

    repo = Path(args.repo)
    now = utc_now_iso()

    universe = [x.strip() for x in args.universe.split(",") if x.strip()] or ["MHG"]

    soak_path = first_existing(
        [
            repo / "args" / "data" / "ops_soak_gate.json",
            repo / "args" / "data" / "ops_soak_gate_latest.json",
            repo / "args" / "data" / "ops_soak_gate_v1.json",
        ]
    )

    reconcile_path = first_existing(
        [
            repo / "args" / "data" / "reconcile_evidence_latest_v2.json",
            repo / "args" / "data" / "reconcile_evidence_latest.json",
            repo / "args" / "data" / "reconcile_latest.json",
        ]
    )

    control_plane_path = repo / "args" / "data" / "control_plane.json"
    signals_path = repo / "args" / "data" / "as_v1_signals_latest.json"

    cursor_path = repo / "args" / "data" / "as_v1.cursor.json"
    latest_path = repo / "args" / "data" / "as_v1_latest.json"

    soak_obj, soak_err = load_json_retry(soak_path, tries=6, sleep_sec=0.05)
    rec_obj, rec_err = load_json_retry(reconcile_path, tries=6, sleep_sec=0.05)
    cp_obj, cp_err = load_json_retry(control_plane_path, tries=3, sleep_sec=0.05)
    sig_obj, sig_err = load_json_retry(signals_path, tries=3, sleep_sec=0.05)

    soak_status = ok_status_from_any(soak_obj)
    reconcile_status = ok_status_from_any(rec_obj)

    soak_ok = soak_status == "OK"
    reconcile_ok = reconcile_status == "OK"
    reconcile_warn = reconcile_status == "WARN"

    # exit-on-warn gates:
    # - ENTER only when soak OK and reconcile OK
    # - EXIT/REDUCE/TP allowed when soak OK and reconcile OK/WARN
    enter_gate_ok = soak_ok and reconcile_ok
    exit_gate_ok = soak_ok and (reconcile_ok or reconcile_warn)

    # keep "gates_ok" semantics as full-green-for-entries
    gates_ok = enter_gate_ok

    mode, mode_warns = get_control_plane_mode(cp_obj)
    execution_mode = get_control_plane_execution_mode(cp_obj)
    allow_entry, allow_exit, policy_warns = policy_from_mode(mode)

    warns: List[str] = []
    warns.extend(mode_warns)
    warns.extend(policy_warns)

    if soak_err:
        warns.append(f"soak_load_err:{soak_err}")
    if rec_err:
        warns.append(f"reconcile_load_err:{rec_err}")
    if cp_err:
        warns.append(f"control_plane_load_err:{cp_err}")
    if sig_err:
        warns.append(f"signals_load_err:{sig_err}")

    if soak_status != "OK":
        warns.append(f"soak_gate_not_ok:{soak_status}")
    if reconcile_status == "WARN":
        warns.append("reconcile_warn")
    if reconcile_status not in ("OK", "WARN"):
        warns.append(f"reconcile_not_ok:{reconcile_status}")
    if execution_mode == "UNKNOWN":
        warns.append("execution_mode_unknown")

    cursor_obj = load_json_retry(cursor_path, tries=1)[0] or {}
    fsm_map: Dict[str, Any] = cursor_obj.get("fsm") or {}

    pos_path = resolve_positions_snapshot_path(repo, rec_obj)
    oo_path = resolve_open_orders_snapshot_path(repo, rec_obj)

    pos_snapshot = None
    if pos_path and pos_path.exists():
        pos_snapshot, _ = load_json_retry(pos_path, tries=2, sleep_sec=0.02)

    sigs = load_signals(sig_obj)

    intents: List[Dict[str, Any]] = []
    for sym in universe:
        pos_qty = (
            load_positions_qty_from_snapshot(pos_snapshot, sym)
            if isinstance(pos_snapshot, dict)
            else 0.0
        )
        open_orders = (
            count_open_orders_from_jsonl(oo_path, sym)
            if (oo_path and oo_path.exists())
            else 0
        )

        sig = sigs.get(sym) if isinstance(sigs, dict) else None
        sig = sig if isinstance(sig, dict) else {}

        gate_tag = "exit_gate" if pos_qty != 0 else "entry_gate"
        gate_ok_for_symbol = exit_gate_ok if pos_qty != 0 else enter_gate_ok

        intent_type, side, qty, confidence, reason, blocked_by = pick_intent(
            sym=sym,
            pos_qty=pos_qty,
            open_orders=open_orders,
            allow_entry=allow_entry,
            allow_exit=allow_exit,
            gate_ok=gate_ok_for_symbol,
            gate_tag=gate_tag,
            sig=sig,
        )

        # FSM update
        fsm_raw = fsm_map.get(sym) or {}
        fsm = FSMState(
            state=str(fsm_raw.get("state") or "WAIT"),
            cooldown_until_utc=fsm_raw.get("cooldown_until_utc"),
            last_transition_utc=fsm_raw.get("last_transition_utc"),
        )
        fsm = transition(
            now, fsm, pos_qty, open_orders, intent_type, int(args.cooldown_sec)
        )

        actionable = False
        if intent_type != "NONE" and ("signals_missing" not in blocked_by):
            if execution_mode in ("PAPER_EXECUTION_ENABLED", "LIVE_ENABLED"):
                actionable = True
            else:
                blocked_by.append("execution_mode_not_enabled")

        intents.append(
            {
                "intent_id": f"ASV1:{now}:{sym}:{intent_type}",
                "ts_utc": now,
                "instrument": sym,
                "type": intent_type,
                "side": side,
                "qty": int(qty),
                "confidence": float(confidence),
                "reason": reason,
                "actionable": actionable,
                "blocked_by": blocked_by,
                # debug helpers
                "pos_qty": float(pos_qty),
                "open_orders": int(open_orders),
                "gate_tag": gate_tag,
                "gate_ok": bool(gate_ok_for_symbol),
            }
        )

        fsm_map[sym] = {
            "state": fsm.state,
            "cooldown_until_utc": fsm.cooldown_until_utc,
            "last_transition_utc": fsm.last_transition_utc,
            "pos_qty": pos_qty,
            "open_orders": open_orders,
        }

    # IMPORTANT: process-level OK if exits are allowed (exit-on-warn).
    status = "OK" if exit_gate_ok else "WARN"
    exit_code = 0 if exit_gate_ok else 1

    out = {
        "schema": "as_v1_latest",
        "ts_utc": now,
        "status": status,
        "exit_code": exit_code,
        "inputs": {
            "ops_soak_gate": str(soak_path),
            "reconcile_latest": str(reconcile_path),
            "control_plane": str(control_plane_path),
            "signals_latest": str(signals_path),
        },
        "sources": {
            "positions_snapshot_path": str(pos_path) if pos_path else None,
            "open_orders_jsonl_path": str(oo_path) if oo_path else None,
        },
        "gates": {
            "soak_status": soak_status,
            "reconcile_status": reconcile_status,
            "enter_gate_ok": enter_gate_ok,
            "exit_gate_ok": exit_gate_ok,
            "gates_ok": gates_ok,
            "mode": mode,
            "allow_entry": allow_entry,
            "allow_exit": allow_exit,
            "execution_mode": execution_mode,
        },
        "warns": warns,
        "fsm": fsm_map,
        "intents": intents,
    }

    save_json(latest_path, out)

    ev_dir = (
        repo
        / "args"
        / "ops_evidence"
        / "as_v1"
        / now.replace(":", "").replace("-", "").replace(".", "")
    )
    save_json(ev_dir / "as_v1_latest.json", out)

    cursor_out = {"schema": "as_v1_cursor", "last_run_utc": now, "fsm": fsm_map}
    save_json(cursor_path, cursor_out)

    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
