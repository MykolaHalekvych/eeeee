from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

_VALID = {"NO_TRADE", "ONLY_EXITS", "ALLOW_NEW_ENTRIES"}


def _norm(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    s = v.strip().upper()
    return s if s in _VALID else None


def _deep_get(d: Dict[str, Any], path: str):
    cur: Any = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


def _evt_type(evt: Dict[str, Any]) -> str:
    for k in ("type", "event_type", "kind", "name"):
        v = evt.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "<no-type>"


def _latest_events_file(data_dir: Path) -> Optional[Path]:
    files = sorted(data_dir.glob("events_run_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    data = repo / "args" / "data"

    f = _latest_events_file(data)
    if not f:
        print(f"[diag] no events_run_*.jsonl in {data}")
        return 2

    first_candidate = None
    types_seen = {}
    scanned = 0

    with f.open("r", encoding="utf-8-sig", errors="replace") as r:
        for line in r:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue

            scanned += 1
            t = _evt_type(evt)
            types_seen[t] = types_seen.get(t, 0) + 1

            # Prefer event that contains MA output
            if isinstance(evt, dict) and ("risk_envelope" in evt) and ("ma_decision" in evt):
                first_candidate = evt
                break

            # Fallback: classic tick marker
            if t.upper() == "TICK":
                first_candidate = evt
                break

            if scanned >= 5000:
                break

    if first_candidate is None:
        print(f"[diag] file={f.name}")
        print(f"[diag] no candidate event with risk_envelope/ma_decision (scanned={scanned})")
        top = sorted(types_seen.items(), key=lambda kv: kv[1], reverse=True)[:12]
        print("[diag] top event types:")
        for k, v in top:
            print(f"  - {k}: {v}")
        return 2

    evt = first_candidate
    exec_gm = _norm(_deep_get(evt, "exec.global_mode") or _deep_get(evt, "ma_input.exec.global_mode"))
    risk_mode = _norm(_deep_get(evt, "risk_envelope.mode"))
    enforced_no_trade = bool(evt.get("enforced_no_trade") or _deep_get(evt, "risk_envelope.enforced_no_trade"))
    ma_decision = evt.get("ma_decision")

    print(f"[diag] file={f.name}")
    print(f"[diag] event_type={_evt_type(evt)}")
    print(f"[diag] exec.global_mode={exec_gm}")
    print(f"[diag] risk_envelope.mode={risk_mode}")
    print(f"[diag] enforced_no_trade={enforced_no_trade}")
    print(f"[diag] ma_decision={ma_decision}")

    if (not enforced_no_trade) and exec_gm == "ALLOW_NEW_ENTRIES" and risk_mode != "ALLOW_NEW_ENTRIES":
        raise SystemExit("[diag] FAIL: exec.global_mode=ALLOW_NEW_ENTRIES but risk_envelope.mode != ALLOW_NEW_ENTRIES")

    if ma_decision == "ALLOW" and risk_mode == "NO_TRADE":
        raise SystemExit("[diag] FAIL: ma_decision=ALLOW while risk_envelope.mode=NO_TRADE")

    print("[diag] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
