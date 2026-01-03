from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        return {"_parse_error": repr(e), "_stdout_head": text[:800]}

def _extract_evidence_dir(obj: Any) -> Optional[Path]:
    if not isinstance(obj, dict):
        return None
    # Common shapes:
    # 1) demo_terminal_proof_pack_v1 -> top-level evidence_dir
    ed = obj.get("evidence_dir")
    if isinstance(ed, str) and ed:
        return Path(ed)
    # 2) nested scenario.json.evidence_dir
    sc = obj.get("scenario")
    if isinstance(sc, dict):
        scj = sc.get("json")
        if isinstance(scj, dict):
            ed2 = scj.get("evidence_dir")
            if isinstance(ed2, str) and ed2:
                return Path(ed2)
    return None

def _detect_ib_code_399(evidence_dir: Path) -> Optional[Dict[str, Any]]:
    # Look for ib_events.jsonl written by terminal_scenarios
    p = evidence_dir / "ib_events.jsonl"
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    # Scan tail for "event":"error" with code 399
    tail = lines[-500:] if len(lines) > 500 else lines
    found: Optional[Dict[str, Any]] = None
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("event") == "error" and o.get("code") == 399:
            found = {
                "code": 399,
                "orderId": o.get("orderId"),
                "reqId": o.get("reqId"),
                "msg": o.get("msg"),
                "ts_utc": o.get("ts_utc"),
            }
            break
    return found

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

    # 1) Run original pack, force --repo for determinism
    rc0, out0, err0 = _run(
        repo,
        ["args.demo.demo_terminal_proof_pack_v1", "--repo", str(repo), *passthru],
        timeout_s=max(60, args.timeout_s),
    )
    scenario_json = _safe_parse_json(out0)

    evidence_dir = _extract_evidence_dir(scenario_json)
    blocked_399 = _detect_ib_code_399(evidence_dir) if (rc0 != 0 and evidence_dir) else None

    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "ok": False,
        "scenario_rc": rc0,
        "scenario": scenario_json,
        "blocked": None,
        "baseline": None,
        "cleanup": None,
        "stderr_tail": (err0 or "")[-1200:],
        "notes": [
            "Wrapper runs demo_terminal_proof_pack_v1 and then performs baseline check/clean (optional).",
            "If ib_events.jsonl contains IB code=399 (delayed placement), wrapper classifies as BLOCKED (exit_code=2).",
            "Baseline clean is cancel-only and still gated by control_plane + stop.flag (same safety rules).",
        ],
    }

    if blocked_399:
        result["blocked"] = {
            "reason": "IBKR_DELAYED_PLACEMENT_399",
            "details": blocked_399,
            "evidence_dir": str(evidence_dir) if evidence_dir else None,
        }

    # If we skip baseline work, just return classification
    if args.no_postflight_baseline:
        result["ok"] = (rc0 == 0)
        print(json.dumps(result, ensure_ascii=False))
        if blocked_399:
            return 2
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
        timeout_s=max(20, args.baseline_timeout_s + 25),
    )
    baseline_json = _safe_parse_json(out_b)
    result["baseline"] = {"rc": rc_b, "json": baseline_json, "stderr_tail": (err_b or "")[-1200:]}

    # 3) Optional baseline clean (cancel-only, gated)
    cleanup_attempted = False
    baseline_after = None
    if (not args.no_postflight_clean) and isinstance(baseline_json, dict):
        oo = baseline_json.get("open_orders")
        if isinstance(oo, dict) and isinstance(oo.get("count"), int) and oo["count"] > 0:
            cleanup_attempted = True
            rc_c, out_c, err_c = _run(
                repo,
                [
                    "args.ops.baseline_cleaner_v1",
                    "--repo", str(repo),
                    "--timeout-s", str(args.baseline_timeout_s),
                    "--wait-s", str(args.baseline_wait_s),
                ],
                timeout_s=max(30, args.baseline_timeout_s + 45),
            )
            cleanup_json = _safe_parse_json(out_c)
            result["cleanup"] = {"rc": rc_c, "json": cleanup_json, "stderr_tail": (err_c or "")[-1200:]}

            # Re-check baseline after cleanup attempt
            rc_b2, out_b2, err_b2 = _run(
                repo,
                [
                    "args.ops.baseline_check_v1",
                    "--repo", str(repo),
                    "--timeout-s", str(args.baseline_timeout_s),
                    "--wait-s", str(args.baseline_wait_s),
                ],
                timeout_s=max(20, args.baseline_timeout_s + 25),
            )
            baseline_after = _safe_parse_json(out_b2)
            result["baseline_after_cleanup"] = {"rc": rc_b2, "json": baseline_after, "stderr_tail": (err_b2 or "")[-1200:]}

    # Decide baseline cleanliness
    def _baseline_count_and_status(bj: Any) -> Tuple[Optional[int], Optional[str]]:
        if not isinstance(bj, dict):
            return None, None
        oo = bj.get("open_orders")
        if not isinstance(oo, dict):
            return None, None
        return oo.get("count") if isinstance(oo.get("count"), int) else None, oo.get("status")

    bj_final = baseline_after if baseline_after is not None else baseline_json
    bcount, bstatus = _baseline_count_and_status(bj_final)

    # Wrapper exit code logic:
    # - If blocked_399: return 2 (blocked), regardless of scenario rc
    # - Else if scenario rc==0 but baseline dirty => return 1 (forces attention)
    # - Else return scenario rc (fail stays fail)
    if blocked_399:
        result["ok"] = False
        result["reason"] = "BLOCKED_MARKET_DELAYED_PLACEMENT"
        print(json.dumps(result, ensure_ascii=False))
        return 2

    if rc0 == 0 and isinstance(bcount, int) and bcount > 0:
        result["ok"] = False
        result["reason"] = "BASELINE_DIRTY_AFTER_SCENARIO"
        result["baseline_status"] = bstatus
        print(json.dumps(result, ensure_ascii=False))
        return 1

    result["ok"] = (rc0 == 0)
    if rc0 != 0 and cleanup_attempted:
        result["notes"].append("Scenario failed/blocked; cleanup attempted as best-effort.")

    print(json.dumps(result, ensure_ascii=False))
    return rc0

if __name__ == "__main__":
    raise SystemExit(main())
