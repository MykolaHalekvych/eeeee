from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

SCHEMA = "demo_terminal_proof_pack_v1c"

def _run(repo: Path, mod_args: List[str], timeout_s: int) -> Tuple[int, str, str]:
    p = subprocess.run(
        [sys.executable, "-m", *mod_args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

def _safe_parse_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"_parse_error": "empty_stdout"}
    try:
        return json.loads(text)
    except Exception as e:
        return {"_parse_error": repr(e), "_stdout_head": text[:500]}

def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--repo", default=".", help="Repo root")
    ap.add_argument("--no-postflight-baseline", action="store_true")
    ap.add_argument("--no-postflight-clean", action="store_true")
    ap.add_argument("--timeout-s", type=int, default=240)
    ap.add_argument("--baseline-timeout-s", type=int, default=45)
    ap.add_argument("--baseline-wait-s", type=int, default=6)
    args, passthru = ap.parse_known_args()

    repo = Path(args.repo).resolve()

    # 1) Run original pack (as-is)
    rc0, out0, err0 = _run(repo, ["args.demo.demo_terminal_proof_pack_v1", *passthru], timeout_s=max(60, args.timeout_s))
    scenario_json = _safe_parse_json(out0)

    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "scenario_rc": rc0,
        "scenario": scenario_json,
        "baseline": None,
        "cleanup": None,
        "stderr_tail": (err0 or "")[-800:],
        "notes": [
            "Wrapper runs demo_terminal_proof_pack_v1 and then performs baseline check/clean (optional).",
            "Baseline clean is cancel-only and still gated by control_plane + stop.flag (same safety rules).",
        ],
    }

    # If we skip baseline work, just pass through
    if args.no_postflight_baseline:
        result["ok"] = (rc0 == 0)
        print(json.dumps(result, ensure_ascii=False))
        return rc0

    # 2) Baseline check (read-only)
    rc_b, out_b, err_b = _run(
        repo,
        [
            "args.ops.baseline_check_v1",
            "--repo", str(repo),
            "--timeout-s", str(args.baseline_timeout_s),
            "--wait-s", str(args.baseline_wait_s),
        ],
        timeout_s=max(20, args.baseline_timeout_s + 20),
    )
    baseline_json = _safe_parse_json(out_b)
    result["baseline"] = {"rc": rc_b, "json": baseline_json, "stderr_tail": (err_b or "")[-800:]}

    # 3) Optional baseline clean (cancel-only, gated)
    cleanup_attempted = False
    if (not args.no_postflight_clean) and isinstance(baseline_json, dict):
        open_orders = (baseline_json.get("open_orders") or {}) if isinstance(baseline_json.get("open_orders"), dict) else {}
        count = open_orders.get("count")
        if isinstance(count, int) and count > 0:
            cleanup_attempted = True
            rc_c, out_c, err_c = _run(
                repo,
                [
                    "args.ops.baseline_cleaner_v1",
                    "--repo", str(repo),
                    "--timeout-s", str(args.baseline_timeout_s),
                    "--wait-s", str(args.baseline_wait_s),
                ],
                timeout_s=max(30, args.baseline_timeout_s + 40),
            )
            cleanup_json = _safe_parse_json(out_c)
            result["cleanup"] = {"rc": rc_c, "json": cleanup_json, "stderr_tail": (err_c or "")[-800:]}

            # Re-check baseline after cleanup
            rc_b2, out_b2, err_b2 = _run(
                repo,
                [
                    "args.ops.baseline_check_v1",
                    "--repo", str(repo),
                    "--timeout-s", str(args.baseline_timeout_s),
                    "--wait-s", str(args.baseline_wait_s),
                ],
                timeout_s=max(20, args.baseline_timeout_s + 20),
            )
            baseline2 = _safe_parse_json(out_b2)
            result["baseline_after_cleanup"] = {"rc": rc_b2, "json": baseline2, "stderr_tail": (err_b2 or "")[-800:]}

    # Decision: scenario must be OK AND baseline must be clean (or blocked reason is acceptable)
    ok_scenario = (rc0 == 0)
    baseline_status = None
    baseline_count = None
    try:
        oo = (result.get("baseline_after_cleanup", result["baseline"])["json"].get("open_orders") or {})
        baseline_status = oo.get("status")
        baseline_count = oo.get("count")
    except Exception:
        pass

    # If scenario succeeded but baseline still dirty => fail wrapper (forces attention)
    if ok_scenario and isinstance(baseline_count, int) and baseline_count > 0:
        result["ok"] = False
        result["reason"] = "BASELINE_DIRTY_AFTER_SCENARIO"
        print(json.dumps(result, ensure_ascii=False))
        return 1

    # Otherwise pass through scenario rc (blocked/failed stays failed)
    result["ok"] = ok_scenario
    if not ok_scenario and cleanup_attempted:
        result["notes"].append("Scenario failed/blocked; cleanup attempted as best-effort.")

    print(json.dumps(result, ensure_ascii=False))
    return rc0 if rc0 != 0 else 0

if __name__ == "__main__":
    raise SystemExit(main())
