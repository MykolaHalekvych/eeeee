from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable


# Stage A: canonical no-action decisions (include live_safety NO_DECISION)
NO_ACTION_DECISIONS = {"UNKNOWN", "NO_TRADE", "NO_DECISION"}


# Stage A: semantic intent kinds (do NOT collapse everything into INTENT_ORDER)
KIND_NONE = "INTENT_NONE"
KIND_CANCEL_ALL = "INTENT_CANCEL_ALL"
KIND_ENTRY = "INTENT_ENTRY"
KIND_EXIT = "INTENT_EXIT"
KIND_REDUCE = "INTENT_REDUCE"
KIND_TAKE_PROFIT = "INTENT_TAKE_PROFIT"

_ALLOWED_KINDS = {
    KIND_NONE,
    KIND_CANCEL_ALL,
    KIND_ENTRY,
    KIND_EXIT,
    KIND_REDUCE,
    KIND_TAKE_PROFIT,
}


@dataclass(frozen=True)
class OrderIntent:
    kind: str
    kind_raw: str
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
            "kind_raw": self.kind_raw,
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
        f.write(
            json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        f.write("\n")


def _norm_decision(x: Any) -> str:
    return str(x or "UNKNOWN").strip().upper().replace("-", "_")


def _u(x: Any) -> str:
    return str(x or "").strip().upper().replace("-", "_")


def _looks_like_noop_action(a: Dict[str, Any]) -> bool:
    """
    Stage A: treat v0+v1 noop consistently.
    - empty dict => noop
    - action/type/kind in known noop values => noop
    """
    if not a:
        return True
    t = _u(a.get("action") or a.get("type") or a.get("kind") or a.get("intent"))
    return t in {"NONE", "NO_ACTION", "NOOP", "HOLD", "SKIP", "DO_NOTHING"}


def _infer_semantic_kind(wa_action: Dict[str, Any]) -> str:
    """
    Stage A: infer intent semantics from WA action fields.
    We accept both v0 stub and any future v1:
    - wa_action["action"] in {"EXIT","REDUCE","ALLOW","NO_ACTION"}
    - wa_action["notes"]["intent"] in {"EXIT","REDUCE","ALLOW","TAKE_PROFIT","ENTRY"}
    - wa_action["intent"] may exist as a direct field
    """
    if not isinstance(wa_action, dict):
        return KIND_NONE

    act = _u(wa_action.get("action"))
    direct_intent = _u(wa_action.get("intent"))
    notes = wa_action.get("notes")
    notes_intent = ""
    if isinstance(notes, dict):
        notes_intent = _u(notes.get("intent"))

    sig = direct_intent or notes_intent or act

    if sig in {"EXIT", "CLOSE"}:
        return KIND_EXIT
    if sig == "REDUCE":
        return KIND_REDUCE
    if sig in {"TAKE_PROFIT", "TP"}:
        return KIND_TAKE_PROFIT

    # "ALLOW" here means allowed to proceed; treat as ENTRY signal placeholder.
    # Real direction/side sizing is NOT decided here (payload stage must still validate).
    if sig == "ALLOW" or sig == "ENTRY" or sig == "ENTER":
        return KIND_ENTRY

    if sig in {"NO_ACTION", "NOOP", "NONE"}:
        return KIND_NONE

    # Unknown action => safest: NONE
    return KIND_NONE


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

    # HALT => cancel-all intent (must survive mode gating later)
    if halted:
        return OrderIntent(
            kind=KIND_CANCEL_ALL,
            kind_raw=KIND_CANCEL_ALL,
            run_id=run_id,
            index=index,
            ts=ts,
            instrument=instrument,
            timeframe=timeframe,
            ma_decision=d,
            wa_action=wa_action,
            reason=f"HALT:{halt_reason or 'kill_switch'}",
        )

    # no-action decisions include live_safety NO_DECISION
    if d in NO_ACTION_DECISIONS:
        return OrderIntent(
            kind=KIND_NONE,
            kind_raw=KIND_NONE,
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
            kind=KIND_NONE,
            kind_raw=KIND_NONE,
            run_id=run_id,
            index=index,
            ts=ts,
            instrument=instrument,
            timeframe=timeframe,
            ma_decision=d,
            wa_action=wa_action,
            reason="WA_NO_ACTION",
        )

    sk = _infer_semantic_kind(wa_action)
    if sk not in _ALLOWED_KINDS:
        sk = KIND_NONE

    return OrderIntent(
        kind=sk,
        kind_raw=sk,
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
    n_entry = 0
    n_exit = 0
    n_reduce = 0
    n_tp = 0
    n_cancel = 0

    if halted:
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
            "intent_entry": 0,
            "intent_exit": 0,
            "intent_reduce": 0,
            "intent_take_profit": 0,
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

        if intent.kind == KIND_NONE:
            n_none += 1
        elif intent.kind == KIND_ENTRY:
            n_entry += 1
        elif intent.kind == KIND_EXIT:
            n_exit += 1
        elif intent.kind == KIND_REDUCE:
            n_reduce += 1
        elif intent.kind == KIND_TAKE_PROFIT:
            n_tp += 1
        else:
            n_cancel += 1

    return {
        "events_total": total,
        "ticks": ticks,
        "intents_written": ticks,
        "intent_none": n_none,
        "intent_entry": n_entry,
        "intent_exit": n_exit,
        "intent_reduce": n_reduce,
        "intent_take_profit": n_tp,
        "intent_cancel_all": n_cancel,
        "out_intents": str(out_intents),
    }


# --- Stage 4.2: mode gating moved into WA core (not demo) ---
from args.wa.mode_gate_v1 import apply_mode_gate_from_report


def enforce_mode_gate(intent: dict, run_report: dict) -> dict:
    # fail-safe: if shapes are wrong -> no trade
    if not isinstance(intent, dict):
        return {
            "kind": KIND_NONE,
            "kind_raw": str(intent),
            "gate_reason": "intent_not_dict",
        }
    if not isinstance(run_report, dict):
        run_report = {}
    return apply_mode_gate_from_report(intent, run_report)
