from __future__ import annotations

import datetime as _dt
import json
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple


ALLOWED_MODES = {"DRY_RUN", "EXIT_ONLY", "FULL"}
DEFAULT_MODE = "DRY_RUN"

EXEC_MODE_PATH_REL = Path("args") / "data" / "execution_mode.json"
STOP_FLAG_PATH_REL = Path("args") / "data" / "stop.flag"


def _utc_now_iso() -> str:
    return _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _norm_u(x: Any) -> str:
    return str(x or "").strip().upper().replace("-", "_")


def _read_json_dict_safe(path: Path) -> Dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def get_execution_mode(repo_root: Path) -> str:
    try:
        p = repo_root / EXEC_MODE_PATH_REL
        if not p.exists():
            return DEFAULT_MODE
        obj = _read_json_dict_safe(p)
        mode = _norm_u(obj.get("mode", DEFAULT_MODE))
        return mode if mode in ALLOWED_MODES else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def is_stop_flag_present(repo_root: Path) -> bool:
    try:
        return (repo_root / STOP_FLAG_PATH_REL).exists()
    except Exception:
        return False


def _looks_like_exit_action(intent_kind_u: str, plan_kind_u: str, payload_kind_u: str, item: Dict[str, Any]) -> bool:
    if intent_kind_u and any(tok in intent_kind_u for tok in ("EXIT", "REDUCE", "CANCEL", "FLATTEN", "CLOSE", "TAKE_PROFIT", "TP")):
        return True
    if plan_kind_u and any(tok in plan_kind_u for tok in ("CANCEL", "CLOSE", "EXIT", "FLATTEN", "REDUCE")):
        return True
    action_class = _norm_u(item.get("action_class"))
    if action_class and any(tok in action_class for tok in ("EXIT", "REDUCE", "CANCEL", "FLATTEN", "CLOSE")):
        return True
    return False


def is_action_allowed(
    *,
    mode: str,
    stop_flag: bool,
    ma_decision: str,
    gate_reason: str,
    intent_kind: str,
    plan_kind: str,
    payload_kind: str,
    sendplan_item: Dict[str, Any] | None = None,
) -> Tuple[bool, str]:
    mode_u = _norm_u(mode)
    if mode_u not in ALLOWED_MODES:
        mode_u = DEFAULT_MODE

    if stop_flag:
        return False, "STOP_FLAG"

    if mode_u == "DRY_RUN":
        return False, "MODE_DRY_RUN"

    ma_u = _norm_u(ma_decision)
    gate = str(gate_reason or "")
    gate_u = gate.upper()

    intent_u = _norm_u(intent_kind)
    plan_u = _norm_u(plan_kind)
    payload_u = _norm_u(payload_kind)

    item = sendplan_item or {}
    is_exit = _looks_like_exit_action(intent_u, plan_u, payload_u, item)

    if is_exit:
        return True, "ALLOW_EXIT"

    if mode_u == "EXIT_ONLY":
        return False, "MODE_EXIT_ONLY_ENTRY_BLOCKED"

    if "ENFORCED_NO_TRADE" in gate_u:
        return False, "ENFORCED_NO_TRADE"

    if ma_u not in ("ALLOW", "REDUCE"):
        return False, f"MA_BLOCKED_{ma_u or 'UNKNOWN'}"

    return True, "ALLOW_FULL"


def apply_block_to_sendplan_inplace(item: Dict[str, Any], reason_code: str, note: str) -> None:
    if "plan_kind_intended" not in item and "plan_kind" in item:
        item["plan_kind_intended"] = item.get("plan_kind")
    if "payload_kind_intended" not in item and "payload_kind" in item:
        item["payload_kind_intended"] = item.get("payload_kind")

    item["blocked"] = True
    item["blocked_reason"] = str(reason_code)
    item["blocked_note"] = str(note or "")
    item["blocked_ts_utc"] = _utc_now_iso()

    item["payload_execute"] = False
    item["plan_kind"] = "SENDPLAN_NONE"
    item["payload_kind"] = "PAYLOAD_NONE"


def exec_events_path(repo_root: Path, run_id: str) -> Path:
    rid = (run_id or "unknown").strip()
    return repo_root / "args" / "data" / f"orders_exec_events_{rid}.jsonl"


def append_exec_event(repo_root: Path, run_id: str, event: Dict[str, Any]) -> None:
    p = exec_events_path(repo_root, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def mk_exec_event(
    *,
    kind: str,
    run_id: str,
    outcome: str,
    mode: str,
    reason_code: str,
    sendplan_item: Dict[str, Any] | None = None,
    details: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    sp = sendplan_item or {}
    return {
        "ts_utc": _utc_now_iso(),
        "event_id": f"{kind}:{uuid.uuid4().hex}",
        "run_id": run_id,
        "kind": str(kind),
        "outcome": str(outcome),
        "mode": _norm_u(mode),
        "reason_code": str(reason_code),
        "plan_kind": sp.get("plan_kind"),
        "payload_kind": sp.get("payload_kind"),
        "payload_execute": bool(sp.get("payload_execute", False)),
        "ma_decision": sp.get("ma_decision") or sp.get("decision") or "",
        "gate_reason": sp.get("gate_reason") or sp.get("reason") or "",
        "details": details or {},
    }


def decorate_ibkr_error_details(details: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(details, dict):
        return details
    code = details.get("error_code")
    try:
        code_i = int(code)
    except Exception:
        code_i = None

    if code_i == 321 and "operator_hint" not in details:
        details["operator_hint"] = (
            "IBKR API is in Read-Only mode (error 321). "
            "Disable Read-Only API in TWS/IB Gateway: Global Configuration → API → Settings → Read-Only API = OFF. "
            "Ensure 'Enable ActiveX and Socket Clients' is ON, then restart TWS/IBG."
        )
    return details
