from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


NO_ACTION_DECISIONS = {"UNKNOWN", "NO_TRADE", "NO-TRADE"}


@dataclass(frozen=True)
class OrderIntent:
    kind: str  # INTENT_NONE | INTENT_ORDER | INTENT_CANCEL_ALL
    run_id: str
    index: Any
    ts: Any
    instrument: str
    timeframe: str
    ma_decision: str
    wa_action: Dict[str, Any]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "run_id": self.run_id,
            "index": self.index,
            "ts": self.ts,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "ma_decision": self.ma_decision,
            "wa_action": self.wa_action,
            "reason": self.reason,
        }


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


def _norm_decision(x: Any) -> str:
    return str(x or "UNKNOWN").upper().replace("-", "_")


def _looks_like_noop_action(a: Dict[str, Any]) -> bool:
    # Conservative heuristics (we don't assume WA schema stability yet)
    if not a:
        return True
    t = str(a.get("type") or a.get("kind") or a.get("action") or "").upper()
    return t in {"NONE", "NO_ACTION", "HOLD", "SKIP", "DO_NOTHING"}


def decide_intent(
    *,
    run_id: str,
    index: Any,
    ts: Any,
    instrument: str,
    timeframe: str,
    ma_decision: Any,
    wa_action: Dict[str, Any],
    halted: bool = False,
    halt_reason: str = "",
) -> OrderIntent:
    d = _norm_decision(ma_decision)

    if halted:
        return OrderIntent(
            kind="INTENT_CANCEL_ALL",
            run_id=run_id,
            index=index,
            ts=ts,
            instrument=instrument,
            timeframe=timeframe,
            ma_decision=d,
            wa_action=wa_action,
            reason=f"HALT:{halt_reason or 'kill_switch'}",
        )

    if d in {x.replace("-", "_") for x in NO_ACTION_DECISIONS}:
        return OrderIntent(
            kind="INTENT_NONE",
            run_id=run_id,
            index=index,
            ts=ts,
            instrument=instrument,
            timeframe=timeframe,
            ma_decision=d,
            wa_action=wa_action,
            reason="MA_NO_ACTION",
        )

    if _looks_like_noop_action(wa_action):
        return OrderIntent(
            kind="INTENT_NONE",
            run_id=run_id,
            index=index,
            ts=ts,
            instrument=instrument,
            timeframe=timeframe,
            ma_decision=d,
            wa_action=wa_action,
            reason="WA_NOOP",
        )

    return OrderIntent(
        kind="INTENT_ORDER",
        run_id=run_id,
        index=index,
        ts=ts,
        instrument=instrument,
        timeframe=timeframe,
        ma_decision=d,
        wa_action=wa_action,
        reason="ALLOW_BY_GATEWAY",
    )


def generate_intents(
    *,
    events_path: Path,
    out_intents: Path,
    run_id: str,
    instrument: str,
    timeframe: str,
    halted: bool = False,
    halt_reason: str = "",
) -> Dict[str, Any]:
    if out_intents.exists():
        out_intents.unlink()

    total = 0
    ticks = 0
    n_none = 0
    n_order = 0
    n_cancel = 0

    if halted:
        # single cancel record is enough for v1
        intent = decide_intent(
            run_id=run_id,
            index=None,
            ts=None,
            instrument=instrument,
            timeframe=timeframe,
            ma_decision="UNKNOWN",
            wa_action={},
            halted=True,
            halt_reason=halt_reason,
        )
        append_jsonl(out_intents, intent.to_dict())
        return {
            "events_total": 0,
            "ticks": 0,
            "intents_written": 1,
            "intent_none": 0,
            "intent_order": 0,
            "intent_cancel_all": 1,
            "out_intents": str(out_intents),
        }

    for ev in iter_jsonl(events_path):
        total += 1
        if ev.get("kind") != "TICK":
            continue
        ticks += 1

        wa_action = ev.get("wa_action")
        if not isinstance(wa_action, dict):
            wa_action = {}

        intent = decide_intent(
            run_id=run_id,
            index=ev.get("index"),
            ts=ev.get("ts"),
            instrument=instrument,
            timeframe=timeframe,
            ma_decision=ev.get("ma_decision"),
            wa_action=wa_action,
            halted=False,
            halt_reason="",
        )
        append_jsonl(out_intents, intent.to_dict())

        if intent.kind == "INTENT_NONE":
            n_none += 1
        elif intent.kind == "INTENT_ORDER":
            n_order += 1
        else:
            n_cancel += 1

    return {
        "events_total": total,
        "ticks": ticks,
        "intents_written": ticks,
        "intent_none": n_none,
        "intent_order": n_order,
        "intent_cancel_all": n_cancel,
        "out_intents": str(out_intents),
    }
# --- Stage 4.2: mode gating moved into WA core (not demo) ---
from args.wa.mode_gate_v1 import apply_mode_gate_from_report

def enforce_mode_gate(intent: dict, run_report: dict) -> dict:
    # fail-safe: if shapes are wrong -> no trade
    if not isinstance(intent, dict):
        return {"kind": "INTENT_NONE", "kind_raw": str(intent), "gate_reason": "intent_not_dict"}
    if not isinstance(run_report, dict):
        run_report = {}
    return apply_mode_gate_from_report(intent, run_report)

