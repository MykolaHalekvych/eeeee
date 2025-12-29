# args/wa/reconcile_open_orders_v0.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = "reconcile_open_orders_v0"
SNAPSHOT_SCHEMA_VERSION = "ibkr_open_orders_snapshot_v0"
SNAPSHOT_GLOB = "ibkr_open_orders_*.jsonl"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _u(x: Any) -> str:
    return str(x or "").strip().upper()


def _digits(s: str) -> str:
    return re.sub(r"\D+", "", s or "")


def _as_int(x: Any) -> Optional[int]:
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        d = _digits(x)
        if d:
            try:
                return int(d)
            except Exception:
                return None
    return None


def _parse_iso_z(s: str) -> Optional[datetime]:
    """
    Parse ISO timestamps safely. Supports trailing Z.
    Returns aware UTC datetime or None.
    """
    try:
        s2 = str(s).strip()
        if not s2:
            return None
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def choose_latest_snapshot(repo_root: Path) -> Optional[Path]:
    data_dir = repo_root / "args" / "data"
    files = sorted(data_dir.glob(SNAPSHOT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _norm_exchange(c: Dict[str, Any]) -> str:
    """
    exchange normalization:
      - exchange OR primaryExchange (fallback)
      - SMART -> "" (wildcard)
    """
    ex = _u(c.get("exchange")) or _u(c.get("primaryExchange"))
    if ex == "SMART":
        return ""
    return ex


def _fut_yyyymm(c: Dict[str, Any]) -> str:
    """
    FUT month guard (YYYYMM):
      - prefer lastTradeDateOrContractMonth, fallback to contractMonth
      - supports YYYYMM or YYYYMMDD (use first 6 digits)
      - returns "" if not available / not parseable
    """
    raw = str(c.get("lastTradeDateOrContractMonth") or c.get("contractMonth") or "").strip()
    d = _digits(raw)
    if len(d) >= 6:
        return d[:6]
    return ""


def _sig_core(c: Dict[str, Any]) -> Optional[str]:
    """
    Core signature (exchange ignored):
      symbol|secType|currency
    Used as "signature_loose".
    """
    sym = _u(c.get("symbol"))
    if not sym:
        return None
    sec = _u(c.get("secType"))
    cur = _u(c.get("currency"))
    return f"{sym}|{sec}|{cur}"


def _sig_strict(c: Dict[str, Any]) -> Optional[str]:
    """
    Strict signature (exchange-specific):
      core|exchange
    Only used when exchange is concrete (not SMART/empty).
    """
    core = _sig_core(c)
    if not core:
        return None
    ex = _norm_exchange(c)
    if not ex:
        return None  # wildcard => strict signature disabled
    return f"{core}|{ex}"


def _filter_hits_contract_sensitive(contract_dict: Dict[str, Any], hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Contract-sensitive filtering on candidate hits.
    Currently: FUT month guard
      - If both sides have YYYYMM -> must match
      - If either side missing -> wildcard (do not filter out)
    """
    sec = _u(contract_dict.get("secType"))
    if sec != "FUT":
        return hits

    want = _fut_yyyymm(contract_dict)
    if not want:
        return hits

    out: List[Dict[str, Any]] = []
    for oo in hits:
        c = oo.get("contract") if isinstance(oo.get("contract"), dict) else {}
        got = _fut_yyyymm(c)
        if got and got != want:
            continue
        out.append(oo)
    return out


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
    by_sig_core: Dict[str, List[Dict[str, Any]]]  # alias/back-compat

    # robustness / validity
    schema_ok: bool
    has_start: bool
    has_end: bool
    parse_errors: int


def load_open_orders_snapshot(path: Path) -> SnapshotIndex:
    ts_start: Optional[datetime] = None
    ts_end: Optional[datetime] = None

    open_orders: List[Dict[str, Any]] = []
    by_conid: Dict[int, List[Dict[str, Any]]] = {}
    by_local: Dict[str, List[Dict[str, Any]]] = {}
    by_sig_strict: Dict[str, List[Dict[str, Any]]] = {}
    by_sig_loose: Dict[str, List[Dict[str, Any]]] = {}

    schema_ok = True
    has_start = False
    has_end = False
    parse_errors = 0

    # Fail-soft open: return empty index instead of crashing
    try:
        f = path.open("r", encoding="utf-8-sig", errors="replace")
    except Exception:
        return SnapshotIndex(
            path=path,
            ts_start=None,
            ts_end=None,
            open_orders=[],
            by_conid={},
            by_local_symbol={},
            by_sig_strict={},
            by_sig_loose={},
            by_sig_core={},
            schema_ok=False,
            has_start=False,
            has_end=False,
            parse_errors=0,
        )

    with f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                parse_errors += 1
                continue
            if not isinstance(obj, dict):
                continue

            # allow both "kind" and "event" (robust to producer differences)
            kind = _u(obj.get("kind") or obj.get("event"))

            if kind == "IBKR_SNAPSHOT_START":
                has_start = True
                sv = obj.get("schema_version")
                if isinstance(sv, str) and sv.strip() and sv.strip() != SNAPSHOT_SCHEMA_VERSION:
                    schema_ok = False
                # ts may be "ts" or "ts_utc"
                ts = obj.get("ts") or obj.get("ts_utc")
                if isinstance(ts, str):
                    ts_start = _parse_iso_z(ts) or ts_start
                continue

            if kind == "IBKR_SNAPSHOT_END":
                has_end = True
                ts = obj.get("ts") or obj.get("ts_utc")
                if isinstance(ts, str):
                    ts_end = _parse_iso_z(ts) or ts_end
                continue

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
                "ts": obj.get("ts") or obj.get("ts_utc"),
            }
            open_orders.append(oo)

            # conId
            conid = _as_int(contract.get("conId"))
            if conid is not None:
                by_conid.setdefault(conid, []).append(oo)

            # localSymbol
            ls = contract.get("localSymbol")
            if isinstance(ls, str) and ls.strip():
                by_local.setdefault(ls.strip().upper(), []).append(oo)

            # strict signature (only if exchange concrete)
            s_strict = _sig_strict(contract)
            if s_strict:
                by_sig_strict.setdefault(s_strict, []).append(oo)

            # loose/core signature
            s_loose = _sig_core(contract)
            if s_loose:
                by_sig_loose.setdefault(s_loose, []).append(oo)

    return SnapshotIndex(
        path=path,
        ts_start=ts_start,
        ts_end=ts_end,
        open_orders=open_orders,
        by_conid=by_conid,
        by_local_symbol=by_local,
        by_sig_strict=by_sig_strict,
        by_sig_loose=by_sig_loose,
        by_sig_core=by_sig_loose,  # alias/back-compat
        schema_ok=schema_ok,
        has_start=has_start,
        has_end=has_end,
        parse_errors=parse_errors,
    )


def snapshot_age_seconds(idx: SnapshotIndex) -> Optional[float]:
    """
    Prefer snapshot ts_end/ts_start; fallback to file mtime.
    Never raises. Returns None only if both timestamp and mtime are unavailable.
    """
    now = datetime.now(timezone.utc)
    ref = idx.ts_end or idx.ts_start
    if ref is not None:
        return float((now - ref).total_seconds())

    # fallback: file mtime
    try:
        mtime = idx.path.stat().st_mtime
        age = now.timestamp() - float(mtime)
        return float(age) if age >= 0 else 0.0
    except Exception:
        return None


def snapshot_validity(idx: SnapshotIndex) -> Tuple[bool, str]:
    """
    Fail-closed snapshot validity for reconcile gate.

    Reasons:
      - snapshot_unreadable (schema_ok=False + no markers)
      - snapshot_invalid_schema
      - snapshot_missing_markers
      - snapshot_empty
    """
    if not idx.schema_ok and not (idx.has_start or idx.has_end):
        return False, "snapshot_unreadable"
    if not idx.schema_ok:
        return False, "snapshot_invalid_schema"
    if not (idx.has_start and idx.has_end):
        return False, "snapshot_missing_markers"
    if not idx.open_orders:
        return False, "snapshot_empty"
    return True, ""


def match_open_orders(idx: SnapshotIndex, contract_dict: Dict[str, Any]) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """
    Match priority (standard / Stage5E.2):
      1) conId
      2) localSymbol
      3) signature_strict: core|exchange (only when exchange concrete)
      4) signature_loose:  core (symbol|secType|currency), SMART wildcard by design
    Returns: (matched, match_by, matches)
    """
    conid = _as_int(contract_dict.get("conId"))
    if conid is not None:
        hits = idx.by_conid.get(conid)
        if hits:
            hits2 = _filter_hits_contract_sensitive(contract_dict, hits)
            if hits2:
                return True, "conId", hits2[:10]

    ls = contract_dict.get("localSymbol")
    if isinstance(ls, str) and ls.strip():
        key = ls.strip().upper()
        hits = idx.by_local_symbol.get(key)
        if hits:
            hits2 = _filter_hits_contract_sensitive(contract_dict, hits)
            if hits2:
                return True, "localSymbol", hits2[:10]

    s_strict = _sig_strict(contract_dict)
    if s_strict:
        hits = idx.by_sig_strict.get(s_strict)
        if hits:
            hits2 = _filter_hits_contract_sensitive(contract_dict, hits)
            if hits2:
                return True, "signature_strict", hits2[:10]

    s_loose = _sig_core(contract_dict)
    if s_loose:
        hits = idx.by_sig_loose.get(s_loose)
        if hits:
            hits2 = _filter_hits_contract_sensitive(contract_dict, hits)
            if hits2:
                return True, "signature_loose", hits2[:10]

    return False, "", []
