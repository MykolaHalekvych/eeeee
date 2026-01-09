from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2

REQUIRED_FINAL_KEYS = [
    "schema",
    "step",
    "repo",
    "run_id",
    "exit_code",
    "ok",
]

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def _read_json(path: Path):
    return json.loads(_read_text(path))

def _safe_resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except Exception:
        return p

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True, help="Path to args\\data\\runs\\<run_id>")
    ap.add_argument("--events-parse", choices=["YES", "NO"], default="YES")
    ap.add_argument("--max-events-lines", type=int, default=500)
    args = ap.parse_args()

    run_dir = _safe_resolve(Path(args.run_dir))
    ts_utc = utc_now()

    errors: list[str] = []
    checks: dict = {}

    def infra(msg: str):
        errors.append(msg)
        return RC_INFRA

    def fail(msg: str):
        errors.append(msg)
        return RC_FAIL

    exit_code = RC_OK

    # Basic existence
    if not run_dir.exists() or not run_dir.is_dir():
        exit_code = infra(f"run_dir_missing_or_not_dir: {run_dir}")
    checks["run_dir"] = str(run_dir)

    final_path = run_dir / "final_report.json"
    events_path = run_dir / "events.jsonl"
    evidence_dir_default = run_dir / "evidence"

    checks["final_report_path"] = str(final_path)
    checks["events_path_default"] = str(events_path)
    checks["evidence_dir_default"] = str(evidence_dir_default)

    final = None
    if exit_code == RC_OK:
        if not final_path.exists():
            exit_code = infra("final_report_missing")
        else:
            try:
                final = _read_json(final_path)
            except Exception as e:
                exit_code = infra(f"final_report_parse_error: {type(e).__name__}: {e}")

    # Validate final_report contract
    if exit_code == RC_OK and isinstance(final, dict):
        missing = [k for k in REQUIRED_FINAL_KEYS if k not in final]
        if missing:
            exit_code = fail(f"final_report_missing_required_keys: {missing}")

    # Resolve declared paths (if present)
    declared_events = None
    declared_evidence = None
    declared_final = None

    if exit_code == RC_OK and isinstance(final, dict):
        declared_events = final.get("events_jsonl")
        declared_evidence = final.get("evidence_dir")
        declared_final = final.get("final_report_json")

        # run_id sanity
        rid = str(final.get("run_id", ""))
        checks["final_run_id"] = rid
        checks["run_dir_name"] = run_dir.name
        if rid and run_dir.name and rid != run_dir.name:
            # Treat mismatch as FAIL (contract violation), not INFRA
            exit_code = fail(f"run_id_mismatch: final.run_id={rid} run_dir.name={run_dir.name}")

    # Exit code / ok consistency
    if exit_code == RC_OK and isinstance(final, dict):
        rc = final.get("exit_code")
        ok = final.get("ok")
        checks["final_exit_code"] = rc
        checks["final_ok"] = ok

        if not isinstance(rc, int) or rc not in (0, 1, 2):
            exit_code = fail(f"invalid_exit_code_value: {rc}")
        if not isinstance(ok, bool):
            exit_code = fail(f"invalid_ok_type: {type(ok).__name__}")
        if exit_code == RC_OK:
            expected_ok = (rc == 0)
            if ok != expected_ok:
                exit_code = fail(f"ok_exit_code_inconsistent: ok={ok} exit_code={rc}")

    # Evidence dir existence
    if exit_code == RC_OK and isinstance(final, dict):
        ev_path = evidence_dir_default if not declared_evidence else Path(str(declared_evidence))
        ev_path = _safe_resolve(ev_path)
        checks["evidence_dir"] = str(ev_path)
        if not ev_path.exists() or not ev_path.is_dir():
            exit_code = infra(f"evidence_dir_missing_or_not_dir: {ev_path}")
        else:
            try:
                # lightweight inventory
                n = sum(1 for _ in ev_path.glob("**/*") if _.is_file())
                checks["evidence_files_count"] = n
            except Exception:
                pass

    # Events file existence + optional parse
    if exit_code == RC_OK and isinstance(final, dict):
        evs_path = events_path if not declared_events else Path(str(declared_events))
        evs_path = _safe_resolve(evs_path)
        checks["events_jsonl"] = str(evs_path)

        if not evs_path.exists() or not evs_path.is_file():
            exit_code = infra(f"events_jsonl_missing_or_not_file: {evs_path}")
        else:
            try:
                size = evs_path.stat().st_size
                checks["events_bytes"] = size
                if size <= 0:
                    exit_code = fail("events_jsonl_empty")
            except Exception:
                pass

            if exit_code == RC_OK and args.events_parse == "YES":
                parsed = 0
                bad = 0
                try:
                    with evs_path.open("r", encoding="utf-8") as f:
                        for i, line in enumerate(f):
                            if i >= int(args.max_events_lines):
                                break
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                if isinstance(obj, dict):
                                    parsed += 1
                                else:
                                    bad += 1
                            except Exception:
                                bad += 1
                    checks["events_parsed_lines"] = parsed
                    checks["events_bad_lines"] = bad
                    if parsed == 0:
                        exit_code = fail("events_jsonl_no_parsable_objects")
                    if bad > 0:
                        exit_code = infra(f"events_jsonl_has_bad_lines: {bad}")
                except Exception as e:
                    exit_code = infra(f"events_jsonl_read_error: {type(e).__name__}: {e}")

    # final_report_json declared path consistency (optional)
    if exit_code == RC_OK and isinstance(final, dict) and declared_final:
        df = _safe_resolve(Path(str(declared_final)))
        checks["final_report_json_declared"] = str(df)
        if df != _safe_resolve(final_path):
            # Not fatal; record as warning-like fail? Keep as FAIL to enforce consistency.
            exit_code = fail(f"final_report_json_path_mismatch: declared={df} actual={_safe_resolve(final_path)}")

    out = {
        "schema": "contract_gate_v1",
        "ts_utc": ts_utc,
        "run_dir": str(run_dir),
        "ok": (exit_code == 0),
        "exit_code": exit_code,
        "checks": checks,
        "errors": errors,
    }

    sys.stdout.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    return exit_code

if __name__ == "__main__":
    raise SystemExit(main())