from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

# We import the AS entrypoint to avoid shelling out; still deterministic.
from args.stage7.as_v1 import main as as_v1_main


def _read_json(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _backup(src: Path, dst: Path) -> None:
    _ensure_parent(dst)
    shutil.copy2(src, dst)


def _restore(dst: Path, src: Path) -> None:
    shutil.copy2(src, dst)


def _make_fake_positions(repo: Path, sym: str, local_symbol: str, position: float) -> Path:
    tmp_dir = repo / "args" / "data" / "_tmp_exit_on_warn_smoke"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    p = tmp_dir / "positions_snapshot_fake.json"
    obj = {
        "schema": "ibkr_positions_snapshot_v0",
        "ts_utc": "TEST",
        "ok": True,
        "rows": [
            {
                "account": "TEST",
                "symbol": sym,
                "localSymbol": local_symbol,
                "secType": "FUT",
                "currency": "USD",
                "position": float(position),
                "avgCost": 14000.0,
            }
        ],
    }
    _write_json(p, obj)
    return p


def _make_fake_open_orders(repo: Path) -> Path:
    tmp_dir = repo / "args" / "data" / "_tmp_exit_on_warn_smoke"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    p = tmp_dir / "open_orders_fake.jsonl"
    p.write_text("", encoding="utf-8")
    return p


def _patch_reconcile_for_smoke(reconcile_path: Path, status: str, pos_path: Path, oo_path: Path) -> Dict[str, Any]:
    ev = _read_json(reconcile_path)
    ev["status"] = status
    ev.setdefault("positions", {})
    ev.setdefault("open_orders", {})
    # AS resolves via positions.out_path and open_orders.jsonl_path
    ev["positions"]["out_path"] = str(pos_path)
    ev["open_orders"]["jsonl_path"] = str(oo_path)
    _write_json(reconcile_path, ev)
    return ev


def _patch_signals(signals_path: Path, sym: str) -> None:
    s = _read_json(signals_path) if signals_path.exists() else {}
    if not isinstance(s, dict):
        s = {}
    if "signals" not in s or not isinstance(s["signals"], dict):
        s["signals"] = {}

    s["signals"][sym] = {
        "exit": True,
        "enter": False,
        "reduce": False,
        "tp": False,
        "confidence": 0.50,
        "qty": 1,
        "side": "SELL",
    }
    _write_json(signals_path, s)


def _find_intent(out: Dict[str, Any], sym: str) -> Optional[Dict[str, Any]]:
    intents = out.get("intents")
    if not isinstance(intents, list):
        return None
    for it in intents:
        if isinstance(it, dict) and it.get("instrument") == sym:
            return it
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--symbol", default="MHG")
    ap.add_argument("--local-symbol", default="MHGG6")
    ap.add_argument("--position", type=float, default=1.0)
    ap.add_argument("--cooldown-sec", type=int, default=900)
    args = ap.parse_args()

    repo = Path(args.repo)

    reconcile_path = repo / "args" / "data" / "reconcile_evidence_latest_v2.json"
    signals_path = repo / "args" / "data" / "as_v1_signals_latest.json"
    out_path = repo / "args" / "data" / "as_v1_latest.json"

    # Require reconcile file to exist; this is an ops demo, not a generator.
    if not reconcile_path.exists():
        print(json.dumps({"ok": False, "reason": "missing_reconcile_latest_v2", "path": str(reconcile_path)}))
        return 2

    # Backups
    bak_dir = repo / "args" / "data" / "_tmp_exit_on_warn_smoke"
    bak_dir.mkdir(parents=True, exist_ok=True)
    rec_bak = bak_dir / "reconcile_evidence_latest_v2.BAK.json"
    sig_bak = bak_dir / "as_v1_signals_latest.BAK.json"

    _backup(reconcile_path, rec_bak)
    if signals_path.exists():
        _backup(signals_path, sig_bak)

    try:
        fake_pos = _make_fake_positions(repo, args.symbol, args.local_symbol, args.position)
        fake_oo = _make_fake_open_orders(repo)

        _patch_reconcile_for_smoke(reconcile_path, "WARN", fake_pos, fake_oo)
        _patch_signals(signals_path, args.symbol)

        # Run AS v1 in-process: args.stage7.as_v1 reads files under repo.
        # We emulate CLI by setting sys.argv minimally.
        import sys

        sys.argv = [
            "as_v1",
            "--repo",
            str(repo),
            "--universe",
            args.symbol,
            "--cooldown-sec",
            str(int(args.cooldown_sec)),
        ]
        rc = as_v1_main()

        out = _read_json(out_path) if out_path.exists() else {}
        it = _find_intent(out, args.symbol) or {}

        result = {
            "ok": True,
            "as_rc": int(rc),
            "as_status": out.get("status"),
            "reconcile_status": (out.get("gates") or {}).get("reconcile_status"),
            "enter_gate_ok": (out.get("gates") or {}).get("enter_gate_ok"),
            "exit_gate_ok": (out.get("gates") or {}).get("exit_gate_ok"),
            "intent_type": it.get("type"),
            "intent_blocked_by": it.get("blocked_by") or [],
            "intent_pos_qty": it.get("pos_qty"),
        }

        # Assertions for PASS
        # - reconcile WARN must allow exits
        # - with synthetic pos!=0 and exit signal -> EXIT intent
        if result["reconcile_status"] != "WARN":
            result["ok"] = False
            result["fail_reason"] = "expected_reconcile_warn"
        elif result["exit_gate_ok"] is not True:
            result["ok"] = False
            result["fail_reason"] = "exit_gate_not_ok_under_warn"
        elif result["intent_type"] != "EXIT":
            result["ok"] = False
            result["fail_reason"] = "expected_exit_intent"
        elif "exit_gate_not_ok" in (",".join(result["intent_blocked_by"])):
            result["ok"] = False
            result["fail_reason"] = "exit_gate_blocked_unexpectedly"

        print(json.dumps(result, ensure_ascii=False))

        return 0 if result["ok"] else 1

    finally:
        # Restore original files
        _restore(reconcile_path, rec_bak)
        if sig_bak.exists():
            _restore(signals_path, sig_bak)

        # Cleanup tmp dir best-effort
        try:
            shutil.rmtree(bak_dir)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
