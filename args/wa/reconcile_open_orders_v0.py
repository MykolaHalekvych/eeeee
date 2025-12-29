# args/wa/reconcile_open_orders_v0.py
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = "reconcile_open_orders_v0"
SNAPSHOT_GLOB = "ibkr_open_orders_*.jsonl"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _parse_iso_z(s: str) -> Optional[datetime]:
    try:
        s2 = s.strip()
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def choose_latest_snapshot(repo_root: Path) -> Optional[Path]:
    data_dir = repo_root / "args" / "data"
    files = sorted(data_dir.glob(SNAPSHOT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _sig_loose(c: Dict[str, Any]) -> Optional[str]:
    """
    Loose signature: symbol|secType|currency
    (exchange ignored completely)
    """
    sym = _u(c.get("symbol"))
    if not sym:
        return None
    sec = _u(c.get("secType"))
    cur = _u(c.get("currency"))
    return f"{sym}|{sec}|{cur}"


def _sig_strict(c: Dict[str, Any]) -> Optional[str]:
    """
    Strict signature: symbol|secType|currency|exchange
    IMPORTANT:
      - exchange=SMART treated as wildcard (empty)
      - if exchange missing, try primaryExchange
    """
    sym = _u(c.get("symbol"))
    if not sym:
        return None
    sec = _u(c.get("secType"))
    cur = _u(c.get("currency"))

    ex = _u(c.get("exchange")) or _u(c.get("primaryExchange"))
    if ex == "SMART":
        ex = ""  # wildcard

    return f"{sym}|{sec}|{cur}|{ex}"


@dataclass(frozen=True)
class SnapshotIndex:
    path: Path
    ts_start: Optional[datetime]
    ts_end: Optional[datetime]
    open_orders: List[Dict[str, Any]]
    by_conid: Dict[int, List[Dict[str, Any]]]
    by_local_symbol: Dict[str, List[Dict[str, Any]]]
    by_sig_strict: Dict[str, List[Dict[str, Any]]]
    by_sig_loose: Dict[str, List[Dict[str, Any]]]


def load_open_orders_snapshot(path: Path) -> SnapshotIndex:
    ts_start: Optional[datetime] = None
    ts_end: Optional[datetime] = None

    open_orders: List[Dict[str, Any]] = []
    by_conid: Dict[int, List[Dict[str, Any]]] = {}
    by_local: Dict[str, List[Dict[str, Any]]] = {}
    by_sig_strict: Dict[str, List[Dict[str, Any]]] = {}
    by_sig_loose: Dict[str, List[Dict[str, Any]]] = {}

    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue

            kind = _u(obj.get("kind"))

            if kind == "IBKR_SNAPSHOT_START":
                ts = obj.get("ts")
                if isinstance(ts, str):
                    ts_start = _parse_iso_z(ts) or ts_start

            if kind == "IBKR_SNAPSHOT_END":
                ts = obj.get("ts")
                if isinstance(ts, str):
                    ts_end = _parse_iso_z(ts) or ts_end

            if kind != "IBKR_OPEN_ORDER":
                continue

            contract = obj.get("contract") if isinstance(obj.get("contract"), dict) else {}
            order = obj.get("order") if isinstance(obj.get("order"), dict) else {}
            order_state = obj.get("order_state") if isinstance(obj.get("order_state"), dict) else {}

            oo: Dict[str, Any] = {
                "order_id": obj.get("order_id"),
                "contract": contract,
                "order": order,
                "order_state": order_state,
                "ts": obj.get("ts"),
            }
            open_orders.append(oo)

            # conId
            conid = contract.get("conId")
            if isinstance(conid, int):
                by_conid.setdefault(conid, []).append(oo)

            # localSymbol
            ls = contract.get("localSymbol")
            if isinstance(ls, str) and ls.strip():
                by_local.setdefault(ls.strip().upper(), []).append(oo)

            # strict signature
            s1 = _sig_strict(contract)
            if s1:
                by_sig_strict.setdefault(s1, []).append(oo)

            # loose signature
            s2 = _sig_loose(contract)
            if s2:
                by_sig_loose.setdefault(s2, []).append(oo)

    return SnapshotIndex(
        path=path,
        ts_start=ts_start,
        ts_end=ts_end,
        open_orders=open_orders,
        by_conid=by_conid,
        by_local_symbol=by_local,
        by_sig_strict=by_sig_strict,
        by_sig_loose=by_sig_loose,
    )


def snapshot_age_seconds(idx: SnapshotIndex) -> Optional[float]:
    ref = idx.ts_end or idx.ts_start
    if ref is None:
        return None
    now = datetime.now(timezone.utc)
    return float((now - ref).total_seconds())


def match_open_orders(idx: SnapshotIndex, contract_dict: Dict[str, Any]) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """
    Match priority:
      1) conId
      2) localSymbol
      3) strict signature: symbol|secType|currency|exchange (SMART wildcard)
      4) loose signature:  symbol|secType|currency (no exchange)
    Returns: (matched, match_by, matches)
    """
    conid = contract_dict.get("conId")
    if isinstance(conid, int):
        hits = idx.by_conid.get(conid)
        if hits:
            return True, "conId", hits[:10]

    ls = contract_dict.get("localSymbol")
    if isinstance(ls, str) and ls.strip():
        key = ls.strip().upper()
        hits = idx.by_local_symbol.get(key)
        if hits:
            return True, "localSymbol", hits[:10]

    s1 = _sig_strict(contract_dict)
    if s1:
        hits = idx.by_sig_strict.get(s1)
        if hits:
            return True, "signature_strict", hits[:10]

    s2 = _sig_loose(contract_dict)
    if s2:
        hits = idx.by_sig_loose.get(s2)
        if hits:
            return True, "signature_loose", hits[:10]

    return False, "", []
