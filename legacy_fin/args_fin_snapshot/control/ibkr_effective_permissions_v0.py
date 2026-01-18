# args/control/ibkr_effective_permissions_v0.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
CONTROL_STATE_PATH = DATA_DIR / "control_state.json"


@dataclass(frozen=True)
class EffectivePermissions:
    force_simulate: bool
    allow_cancel_all: bool
    allow_entry_orders: bool
    allow_exit_orders: bool
    explanation: str = ""


def _u(x: Any) -> str:
    return str(x or "").strip().upper().replace("-", "_")


def _read_control_state() -> Dict[str, Any]:
    if not CONTROL_STATE_PATH.exists():
        return {}
    try:
        obj = json.loads(
            CONTROL_STATE_PATH.read_text(encoding="utf-8-sig", errors="replace")
        )
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _stage5_override_allows(rec: Dict[str, Any]) -> bool:
    """
    STRICT Stage5 override:
    - enabled only if control_state.stage5_test_override == true
    - kind == SENDPLAN_ORDER
    - idempotency_key starts with STAGE5_TEST_
    - BUY LMT qty=1
    - lmtPrice <= stage5_test_max_lmt_price (default 0.05)
    """
    cs = _read_control_state()
    if not bool(cs.get("stage5_test_override") is True):
        return False

    try:
        max_lmt = float(cs.get("stage5_test_max_lmt_price", 0.05))
    except Exception:
        max_lmt = 0.05
    if max_lmt <= 0:
        max_lmt = 0.05

    kind = _u(rec.get("kind"))
    if kind != "SENDPLAN_ORDER":
        return False

    ik = str(rec.get("idempotency_key") or "").strip()
    if not ik.startswith("STAGE5_TEST_"):
        return False

    od = rec.get("order") if isinstance(rec.get("order"), dict) else {}
    action = _u(od.get("action"))
    otype = _u(od.get("orderType"))

    try:
        qty = float(od.get("totalQuantity") or 0)
    except Exception:
        qty = 0.0

    try:
        lmt = float(od.get("lmtPrice"))
    except Exception:
        lmt = None

    if (
        action == "BUY"
        and otype == "LMT"
        and qty == 1.0
        and lmt is not None
        and lmt <= max_lmt
    ):
        return True

    return False


def compute_effective_permissions(
    exec_mode: str, run_mode: str
) -> EffectivePermissions:
    em = _u(exec_mode)
    rm = _u(run_mode)

    # Default safe posture
    allow_cancel_all = True
    allow_entry = False
    allow_exit = False
    force_sim = False

    if em == "DRY_RUN":
        force_sim = True
        return EffectivePermissions(
            force_simulate=True,
            allow_cancel_all=True,
            allow_entry_orders=False,
            allow_exit_orders=False,
            explanation="exec_mode=DRY_RUN => force_simulate (no live IBKR calls)",
        )

    # EXIT_ONLY / FULL
    if rm == "NO_TRADE":
        # NO_TRADE blocks everything except cancel_all
        return EffectivePermissions(
            force_simulate=False,
            allow_cancel_all=True,
            allow_entry_orders=False,
            allow_exit_orders=False,
            explanation=f"risk_mode=NO_TRADE => block all orders; allow CANCEL_ALL only; exec_mode={em}",
        )

    if em == "EXIT_ONLY":
        # ONLY_EXITS / ALLOW_NEW_ENTRIES: still block entry in EXIT_ONLY
        allow_exit = True if rm in {"ONLY_EXITS", "ALLOW_NEW_ENTRIES"} else False
        return EffectivePermissions(
            force_simulate=False,
            allow_cancel_all=True,
            allow_entry_orders=False,
            allow_exit_orders=allow_exit,
            explanation=f"exec_mode=EXIT_ONLY => block ENTRY orders (EXIT only); risk_mode={rm}",
        )

    if em == "FULL":
        # FULL respects run_mode
        if rm == "ALLOW_NEW_ENTRIES":
            allow_entry = True
            allow_exit = True
        elif rm == "ONLY_EXITS":
            allow_entry = False
            allow_exit = True
        return EffectivePermissions(
            force_simulate=False,
            allow_cancel_all=True,
            allow_entry_orders=allow_entry,
            allow_exit_orders=allow_exit,
            explanation=f"exec_mode=FULL; risk_mode={rm}",
        )

    # Unknown => safest
    return EffectivePermissions(
        force_simulate=True,
        allow_cancel_all=True,
        allow_entry_orders=False,
        allow_exit_orders=False,
        explanation=f"unknown exec_mode={em} => force_simulate",
    )


def allowed_sendplan_record(
    perms: EffectivePermissions, rec: Dict[str, Any]
) -> Tuple[bool, str]:
    kind = _u(rec.get("kind"))

    if kind == "SENDPLAN_CANCEL_ALL":
        return (
            bool(perms.allow_cancel_all),
            "CANCEL_ALL_ALLOWED" if perms.allow_cancel_all else "CANCEL_ALL_BLOCKED",
        )

    if kind == "SENDPLAN_ORDER":
        # Stage5 override can allow a very strict test ENTRY even in NO_TRADE.
        if _stage5_override_allows(rec):
            return True, "STAGE5_OVERRIDE_ALLOW_ENTRY"

        # Otherwise, treat all orders as "entry" for now (safe).
        if perms.allow_entry_orders:
            return True, "ENTRY_ALLOWED"
        return False, "BLOCKED_ENTRY_BY_MODE"

    return False, "UNKNOWN_KIND"
