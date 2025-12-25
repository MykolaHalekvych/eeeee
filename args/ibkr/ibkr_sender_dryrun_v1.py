from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    yield obj
            except Exception:
                continue


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
        f.write("\n")


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _norm(x: Any) -> str:
    return str(x or "").strip()


def _require(d: Dict[str, Any], k: str) -> Any:
    if k not in d:
        raise ValueError(f"missing field: {k}")
    return d[k]


def _validate_contract(c: Any) -> List[str]:
    errs: List[str] = []
    if not isinstance(c, dict):
        return ["contract not a dict"]
    if c.get("conId") is None and not c.get("localSymbol"):
        errs.append("contract missing conId/localSymbol")
    if not c.get("secType"):
        errs.append("contract missing secType")
    if not c.get("exchange"):
        errs.append("contract missing exchange")
    return errs


def _validate_order(o: Any) -> List[str]:
    errs: List[str] = []
    if not isinstance(o, dict):
        return ["order not a dict"]
    if str(o.get("action") or "").upper() not in {"BUY", "SELL"}:
        errs.append("order.action not BUY/SELL")
    if not o.get("orderType"):
        errs.append("order.orderType missing")
    try:
        q = int(o.get("totalQuantity"))
        if q <= 0:
            errs.append("order.totalQuantity <=
