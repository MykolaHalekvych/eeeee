# args/demo/demo_ibkr_stage8_readiness_v0.py
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


REPO_ROOT = Path(".").resolve()
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"


def _read_text_if_exists(p: Path) -> Optional[str]:
    return p.read_text(encoding="utf-8-sig") if p.exists() else None


def _write_text(p: Path, s: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")


def _write_json(p: Path, obj: Any) -> None:
    _write_text(p, json.dumps(obj, ensure_ascii=False, indent=2))


def _write_jsonl(p: Path, records: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _load_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _send_key(run_id: str, idempotency_key: str) -> str:
    return f"{run_id}:{idempotency_key}"


def _ledger_get_state(ledger_path: Path, send_key: str) -> Optional[str]:
    if not ledger_path.exists():
        return None
    obj = _load_json(ledger_path)
    if not isinstance(obj, dict):
        return None
    items = obj.get("items")
    if (
        isinstance(items, dict)
        and send_key in items
        and isinstance(items[send_key], dict)
    ):
        return items[send_key].get("state")
    return None


def _print_ledger_keys(ledger_path: Path, limit: int = 10) -> None:
    if not ledger_path.exists():
        print("[DEBUG] ledger missing:", ledger_path)
        return
    obj = _load_json(ledger_path)
    items = obj.get("items") if isinstance(obj, dict) else None
    if not isinstance(items, dict):
        print("[DEBUG] ledger has no items dict")
        return
    keys = list(items.keys())
    print(f"[DEBUG] ledger keys (n={len(keys)}), head={keys[:limit]}")


def _run_sender(run_id: str) -> None:
    subprocess.run(
        ["py", "-3.11", "-m", "args.ibkr.ibkr_sender_real_v1", "--run-id", run_id],
        check=True,
    )


def _mk_run_id(tag: str) -> str:
    return f"DEMO_STAGE8_{tag}_" + datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def main() -> None:
    execution_mode_path = DATA_DIR / "execution_mode.json"
    control_state_path = DATA_DIR / "control_state.json"

    old_exec_mode = _read_text_if_exists(execution_mode_path)
    old_control_state = _read_text_if_exists(control_state_path)

    # demo ledger (не трогаем боевой)
    demo_ledger = DATA_DIR / "ibkr_exec_ledger_v1_demo.json"

    try:
        # базовый control_state: armed true, simulate false (DRY_RUN должен форсить simulate)
        base_control_state: Dict[str, Any] = {
            "armed": True,
            "simulate": False,
            "k_limit_orders": 1,
            "run_limit_orders": 1,
            "ledger_path": str(demo_ledger),
            "ibkr_preflight_timeout_s": 3.0,
        }

        # -------------------------
        # Scenario A:
        # DRY_RUN + ALLOW_NEW_ENTRIES => SENT_SIM
        # -------------------------
        run_id_a = _mk_run_id("A")
        idem_a = "demo_stage8_order_1"
        sk_a = _send_key(run_id_a, idem_a)

        _write_json(execution_mode_path, {"mode": "DRY_RUN"})
        _write_json(control_state_path, {**base_control_state, "simulate": False})

        _write_json(
            LOGS_DIR / f"run_report_{run_id_a}_paper.json",
            {
                "run_id": run_id_a,
                "risk_envelope": {"mode": "ALLOW_NEW_ENTRIES"},
            },
        )
        _write_jsonl(
            DATA_DIR / f"orders_sendplan_{run_id_a}.jsonl",
            [
                {
                    "kind": "SENDPLAN_ORDER",
                    "idempotency_key": idem_a,
                    "contract": {
                        "symbol": "MHG",
                        "secType": "FUT",
                        "exchange": "COMEX",
                        "currency": "USD",
                    },
                    "order": {
                        "action": "BUY",
                        "orderType": "MKT",
                        "totalQuantity": 1,
                        "tif": "DAY",
                        "transmit": False,
                    },
                }
            ],
        )

        _run_sender(run_id_a)
        st_a = _ledger_get_state(demo_ledger, sk_a)
        if st_a is None:
            _print_ledger_keys(demo_ledger)
        assert st_a in {"SENT_SIM", "SENT_REAL"}, (
            f"Scenario A expected SENT_SIM (or SENT_REAL), got {st_a}"
        )
        print(f"[OK] Scenario A: {sk_a} => {st_a}")

        # Dedup A (same run_id + idempotency_key)
        _run_sender(run_id_a)
        st_a2 = _ledger_get_state(demo_ledger, sk_a)
        assert st_a2 == st_a, f"Dedup A expected same state, got {st_a2} vs {st_a}"
        print(f"[OK] Dedup A: {sk_a} => {st_a2}")

        # -------------------------
        # Scenario B:
        # EXIT_ONLY + ALLOW_NEW_ENTRIES + ENTRY order => BLOCKED (no ledger entry)
        # -------------------------
        run_id_b = _mk_run_id("B")
        idem_b = "demo_stage8_entry_should_block"
        sk_b = _send_key(run_id_b, idem_b)

        _write_json(execution_mode_path, {"mode": "EXIT_ONLY"})
        _write_json(control_state_path, {**base_control_state, "simulate": False})

        _write_json(
            LOGS_DIR / f"run_report_{run_id_b}_paper.json",
            {
                "run_id": run_id_b,
                "risk_envelope": {"mode": "ALLOW_NEW_ENTRIES"},
            },
        )
        _write_jsonl(
            DATA_DIR / f"orders_sendplan_{run_id_b}.jsonl",
            [
                {
                    "kind": "SENDPLAN_ORDER",
                    "idempotency_key": idem_b,
                    "contract": {
                        "symbol": "MHG",
                        "secType": "FUT",
                        "exchange": "COMEX",
                        "currency": "USD",
                    },
                    "order": {
                        "action": "BUY",
                        "orderType": "MKT",
                        "totalQuantity": 1,
                        "tif": "DAY",
                        "transmit": False,
                    },
                    # намеренно без intent/meta.intent => считается ENTRY => должен быть blocked в EXIT_ONLY
                }
            ],
        )

        _run_sender(run_id_b)
        st_b = _ledger_get_state(demo_ledger, sk_b)
        assert st_b is None, f"Scenario B expected blocked (no ledger item), got {st_b}"
        print(f"[OK] Scenario B blocked: no ledger item for {sk_b}")

        # -------------------------
        # Scenario C:
        # FULL + NO_TRADE => BLOCKED (no ledger entry)
        # -------------------------
        run_id_c = _mk_run_id("C")
        idem_c = "demo_stage8_full_notrade_should_block"
        sk_c = _send_key(run_id_c, idem_c)

        _write_json(execution_mode_path, {"mode": "FULL"})
        _write_json(control_state_path, {**base_control_state, "simulate": False})

        _write_json(
            LOGS_DIR / f"run_report_{run_id_c}_paper.json",
            {
                "run_id": run_id_c,
                "risk_envelope": {"mode": "NO_TRADE"},
            },
        )
        _write_jsonl(
            DATA_DIR / f"orders_sendplan_{run_id_c}.jsonl",
            [
                {
                    "kind": "SENDPLAN_ORDER",
                    "idempotency_key": idem_c,
                    "contract": {
                        "symbol": "MHG",
                        "secType": "FUT",
                        "exchange": "COMEX",
                        "currency": "USD",
                    },
                    "order": {
                        "action": "BUY",
                        "orderType": "MKT",
                        "totalQuantity": 1,
                        "tif": "DAY",
                        "transmit": False,
                    },
                }
            ],
        )

        _run_sender(run_id_c)
        st_c = _ledger_get_state(demo_ledger, sk_c)
        assert st_c is None, f"Scenario C expected blocked (no ledger item), got {st_c}"
        print(f"[OK] Scenario C blocked: no ledger item for {sk_c}")

        print("OK: Stage 8 readiness scenarios passed")

    finally:
        # restore operator-local files
        if old_exec_mode is None:
            if execution_mode_path.exists():
                execution_mode_path.unlink()
        else:
            _write_text(execution_mode_path, old_exec_mode)

        if old_control_state is None:
            if control_state_path.exists():
                control_state_path.unlink()
        else:
            _write_text(control_state_path, old_control_state)


if __name__ == "__main__":
    main()
