from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _run_capture(cmd: List[str], cwd: Path) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _parse_last_json(stdout: str) -> Dict[str, Any]:
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "error": "no_stdout"}
    try:
        return json.loads(lines[-1])
    except Exception:
        return {"ok": False, "error": "stdout_not_json", "stdout_tail": lines[-1]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument(
        "--scenario",
        required=True,
        choices=["scenario_cancelled_v1", "scenario_rejected_v1", "scenario_fill_v1"],
    )
    ap.add_argument("--contract-json", required=True)
    ap.add_argument("--symbol", default="MHG")

    # pass-through flags for terminal scenario module
    ap.add_argument("--confirm-paper", action="store_true")
    ap.add_argument("--confirm-fill", default="NO")
    ap.add_argument("--confirm-roundtrip", default="NO")
    ap.add_argument("--roundtrip-qty", type=float, default=1.0)
    ap.add_argument("--lmt-price", type=float, default=1.0)

    # reconcile pack launcher
    ap.add_argument(
        "--reconcile-cmd", default=r"scripts\run_stage5_reconcile_evidence_pack.cmd"
    )
    ap.add_argument("--skip-reconcile", action="store_true")

    args = ap.parse_args()

    repo = Path(args.repo).resolve()

    # 1) Run terminal scenario
    scen_cmd = [
        "py",
        "-3.11",
        "-m",
        "args.stage5.terminal_scenarios.terminal_scenarios_v1",
        "--repo",
        str(repo),
        "--scenario",
        args.scenario,
        "--contract-json",
        args.contract_json,
        "--symbol",
        args.symbol,
    ]

    # optional flags
    if args.confirm_paper:
        scen_cmd.append("--confirm-paper")
    if args.scenario == "scenario_fill_v1":
        scen_cmd += ["--confirm-fill", str(args.confirm_fill)]
        scen_cmd += ["--confirm-roundtrip", str(args.confirm_roundtrip)]
        scen_cmd += ["--roundtrip-qty", str(args.roundtrip_qty)]
    if args.scenario == "scenario_cancelled_v1":
        scen_cmd += ["--lmt-price", str(args.lmt_price)]

    scen_rc, scen_out, scen_err = _run_capture(scen_cmd, cwd=repo)
    scen_json = _parse_last_json(scen_out)

    evidence_dir = (
        Path(str(scen_json.get("evidence_dir") or "")).resolve()
        if scen_json.get("evidence_dir")
        else None
    )
    if evidence_dir:
        _write_json(evidence_dir / "proof_pack_scenario_cmd.json", {"cmd": scen_cmd})
        (evidence_dir / "proof_pack_scenario_stdout.txt").write_text(
            scen_out, encoding="utf-8"
        )
        (evidence_dir / "proof_pack_scenario_stderr.txt").write_text(
            scen_err, encoding="utf-8"
        )

    # 2) Run reconcile pack (safe: should not place orders)
    reconcile_json: Optional[Dict[str, Any]] = None
    rec_rc: Optional[int] = None

    if not args.skip_reconcile:
        rec_cmd_path = (repo / args.reconcile_cmd).resolve()
        rec_cmd = ["cmd.exe", "/c", str(rec_cmd_path)]
        rec_rc, rec_out, rec_err = _run_capture(rec_cmd, cwd=repo)

        # capture logs
        if evidence_dir:
            _write_json(
                evidence_dir / "proof_pack_reconcile_cmd.json", {"cmd": rec_cmd}
            )
            (evidence_dir / "proof_pack_reconcile_stdout.txt").write_text(
                rec_out, encoding="utf-8"
            )
            (evidence_dir / "proof_pack_reconcile_stderr.txt").write_text(
                rec_err, encoding="utf-8"
            )

        # load latest reconcile evidence, if present
        latest = repo / "args" / "data" / "reconcile_evidence_latest_v2.json"
        last_fail = repo / "args" / "data" / "reconcile_evidence_last_fail_v2.json"
        if latest.exists():
            reconcile_json = _read_json(latest)
            if evidence_dir:
                _write_json(
                    evidence_dir / "reconcile_evidence_latest_v2.json", reconcile_json
                )
        elif last_fail.exists():
            reconcile_json = _read_json(last_fail)
            if evidence_dir:
                _write_json(
                    evidence_dir / "reconcile_evidence_last_fail_v2.json",
                    reconcile_json,
                )

    # 3) Final summary
    # Scenario PASS: rc==0 and json.ok==true
    scen_ok = (scen_rc == 0) and bool(scen_json.get("ok") is True)
    scen_blocked = (scen_rc == 2) or (int(scen_json.get("exit_code") or 0) == 2)

    # Reconcile PASS: status in OK|WARN (if present)
    rec_status = (reconcile_json or {}).get("status") if reconcile_json else None
    rec_ok = (rec_status in ("OK", "WARN")) if reconcile_json else False

    out = {
        "schema": "demo_terminal_proof_pack_v1",
        "ok": bool(scen_ok and (rec_ok or args.skip_reconcile)),
        "scenario": {
            "rc": scen_rc,
            "json": scen_json,
            "blocked": scen_blocked,
        },
        "reconcile": {
            "rc": rec_rc,
            "status": rec_status,
            "ok": rec_ok,
        },
        "evidence_dir": str(evidence_dir) if evidence_dir else None,
        "notes": [
            "This runner never changes control_plane.json; it only executes commands.",
            "If scenario is blocked (DRYRUN), reconcile is still attempted unless --skip-reconcile is set.",
        ],
    }

    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0 if out["ok"] else (2 if scen_blocked else 1)


if __name__ == "__main__":
    raise SystemExit(main())
