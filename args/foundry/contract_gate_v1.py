from __future__ import annotations

import argparse
import json
import sys
import codecs
from pathlib import Path
from datetime import datetime, timezone

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2

# Contract profiles by final_report["schema"]
PROFILES = {
    # Produced by scripts/run_suite_soak_v1.ps1 (final_report.json may omit ok/exit_code)
    "suite_soak_v1": {
        "required": [
            "schema",
            "step",
            "repo",
            "run_id",
            "run_dir",
            "evidence_dir",
            "events_jsonl",
            "final_report_json",
            "summary",
        ],
        "id_key": "run_id",
        "step_key": "step",
        "requires_ok_exit": False,
        "derive_ok_exit_from_summary": True,
    },
    # Produced by scripts/run_family_test_suite_v1.ps1
    "family_test_suite_v1": {
        "required": [
            "schema",
            "ts_utc",
            "repo",
            "ok",
            "exit_code",
            "suite_run_id",
            "suite_run_dir",
            "evidence_dir",
            "events_jsonl",
            "final_report_json",
        ],
        "id_key": "suite_run_id",
        "step_value": "family_suite",
        "requires_ok_exit": True,
        "derive_ok_exit_from_summary": False,
    },
    # Produced by args.foundry build/release pipeline
    "factory_final_report_v1": {
        "required": [
            "schema",
            "ok",
            "exit_code",
            "run_id",
            "run_dir",
            "evidence_dir",
            "events_jsonl",
            "final_report_json",
        ],
        "id_key": "run_id",
        "step_value": "factory_final_report",
        "requires_ok_exit": True,
        "derive_ok_exit_from_summary": False,
    },
}

DEFAULT_PROFILE = {
    "required": ["schema", "ok", "exit_code"],
    "id_key": "run_id",
    "step_key": "step",
    "requires_ok_exit": True,
    "derive_ok_exit_from_summary": False,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except Exception:
        return p


def _read_json_allow_utf8_bom(path: Path):
    """
    Read JSON as UTF-8, but tolerate UTF-8 BOM by stripping it.
    Returns (obj, had_utf8_bom).
    """
    b = path.read_bytes()
    had_bom = b.startswith(codecs.BOM_UTF8)
    if had_bom:
        b = b[len(codecs.BOM_UTF8) :]
    text = b.decode("utf-8")
    return json.loads(text), had_bom


def _derive_ok_exit_from_summary(summary: object):
    """
    Derive (ok, exit_code) from suite_soak_v1 summary.
    Rules:
      - if infra > 0 -> exit_code=2, ok=False
      - elif failed > 0 -> exit_code=1, ok=False
      - else if ok==True -> exit_code=0, ok=True
      - fallback -> (False, 1)
    """
    if not isinstance(summary, dict):
        return False, 1

    s_ok = summary.get("ok")
    failed = summary.get("failed", 0)
    infra = summary.get("infra", 0)

    try:
        failed_i = int(failed)
    except Exception:
        failed_i = 0

    try:
        infra_i = int(infra)
    except Exception:
        infra_i = 0

    if infra_i > 0:
        return False, 2
    if failed_i > 0:
        return False, 1
    if s_ok is True:
        return True, 0
    return False, 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--run-dir", required=True, help="Path to args\\data\\runs\\<run_id>"
    )
    ap.add_argument("--events-parse", choices=["YES", "NO"], default="YES")
    ap.add_argument("--max-events-lines", type=int, default=500)
    ap.add_argument(
        "--bom-strict",
        choices=["YES", "NO"],
        default="NO",
        help="If YES, treat UTF-8 BOM in JSON artifacts as contract FAIL.",
    )
    args = ap.parse_args()

    run_dir = _safe_resolve(Path(args.run_dir))
    ts_utc = utc_now()

    errors: list[str] = []
    checks: dict = {}

    exit_code = RC_OK

    def set_fail(msg: str):
        nonlocal exit_code
        errors.append(msg)
        if exit_code == RC_OK:
            exit_code = RC_FAIL

    def set_infra(msg: str):
        nonlocal exit_code
        errors.append(msg)
        exit_code = RC_INFRA

    # Basic existence
    if not run_dir.exists() or not run_dir.is_dir():
        set_infra(f"run_dir_missing_or_not_dir: {run_dir}")

    checks["run_dir"] = str(run_dir)

    final_path = run_dir / "final_report.json"
    events_path_default = run_dir / "events.jsonl"
    evidence_dir_default = run_dir / "evidence"

    checks["final_report_path"] = str(final_path)
    checks["events_path_default"] = str(events_path_default)
    checks["evidence_dir_default"] = str(evidence_dir_default)

    final = None
    final_bom = False

    # Load final_report.json
    if exit_code == RC_OK:
        if not final_path.exists():
            set_infra("final_report_missing")
        else:
            try:
                final, final_bom = _read_json_allow_utf8_bom(final_path)
                checks["final_report_utf8_bom"] = bool(final_bom)
                if final_bom and args.bom_strict == "YES":
                    set_fail("final_report_has_utf8_bom")
            except Exception as e:
                set_infra(f"final_report_parse_error: {type(e).__name__}: {e}")

    # Profile selection
    profile = DEFAULT_PROFILE
    schema = None
    if exit_code == RC_OK and isinstance(final, dict):
        schema = str(final.get("schema", ""))
        checks["final_schema"] = schema
        profile = PROFILES.get(schema, DEFAULT_PROFILE)
        checks["contract_profile"] = schema if schema in PROFILES else "DEFAULT"

    # Required keys by profile
    if exit_code == RC_OK and isinstance(final, dict):
        missing = [k for k in profile["required"] if k not in final]
        if missing:
            set_fail(f"final_report_missing_required_keys: {missing}")

    # Identify run_id (schema-aware)
    derived_run_id = None
    if exit_code == RC_OK and isinstance(final, dict):
        id_key = profile.get("id_key", "run_id")
        derived_run_id = (
            final.get(id_key) or final.get("run_id") or final.get("suite_run_id")
        )
        checks["derived_id_key"] = id_key
        checks["derived_run_id"] = (
            str(derived_run_id) if derived_run_id is not None else None
        )
        checks["run_dir_name"] = run_dir.name

        if derived_run_id is None:
            set_fail("missing_run_identifier")
        else:
            # Compare run_dir name to derived run id (strict for our run layout)
            if str(derived_run_id) != run_dir.name:
                set_fail(
                    f"run_id_mismatch: derived_run_id={derived_run_id} run_dir.name={run_dir.name}"
                )

    # Step derivation (optional)
    if exit_code == RC_OK and isinstance(final, dict):
        step = None
        if "step_key" in profile and profile["step_key"] in final:
            step = final.get(profile["step_key"])
        elif "step_value" in profile:
            step = profile["step_value"]
        else:
            step = final.get("step") or schema
        checks["derived_step"] = str(step) if step is not None else None

    # ok/exit_code consistency or derivation
    if exit_code == RC_OK and isinstance(final, dict):
        requires_ok_exit = bool(profile.get("requires_ok_exit", True))

        if requires_ok_exit:
            rc = final.get("exit_code")
            ok = final.get("ok")
            checks["final_exit_code"] = rc
            checks["final_ok"] = ok

            if not isinstance(rc, int) or rc not in (0, 1, 2):
                set_fail(f"invalid_exit_code_value: {rc}")
            if not isinstance(ok, bool):
                set_fail(f"invalid_ok_type: {type(ok).__name__}")
            if exit_code == RC_OK:
                expected_ok = rc == 0
                if ok != expected_ok:
                    set_fail(f"ok_exit_code_inconsistent: ok={ok} exit_code={rc}")
        else:
            # suite_soak_v1: derive from summary
            if profile.get("derive_ok_exit_from_summary", False):
                ok_d, rc_d = _derive_ok_exit_from_summary(final.get("summary"))
                checks["derived_ok"] = ok_d
                checks["derived_exit_code"] = rc_d

    # Declared paths
    declared_events = None
    declared_evidence = None
    declared_final = None
    if exit_code == RC_OK and isinstance(final, dict):
        declared_events = final.get("events_jsonl")
        declared_evidence = final.get("evidence_dir")
        declared_final = final.get("final_report_json")

    # Evidence dir existence
    if exit_code == RC_OK and isinstance(final, dict):
        ev_path = (
            evidence_dir_default
            if not declared_evidence
            else Path(str(declared_evidence))
        )
        ev_path = _safe_resolve(ev_path)
        checks["evidence_dir"] = str(ev_path)

        if not ev_path.exists() or not ev_path.is_dir():
            set_infra(f"evidence_dir_missing_or_not_dir: {ev_path}")
        else:
            try:
                n = sum(1 for _ in ev_path.glob("**/*") if _.is_file())
                checks["evidence_files_count"] = n
            except Exception:
                pass

    # Events file existence + optional parse
    if exit_code == RC_OK and isinstance(final, dict):
        evs_path = (
            events_path_default if not declared_events else Path(str(declared_events))
        )
        evs_path = _safe_resolve(evs_path)
        checks["events_jsonl"] = str(evs_path)

        if not evs_path.exists() or not evs_path.is_file():
            set_infra(f"events_jsonl_missing_or_not_file: {evs_path}")
        else:
            try:
                size = evs_path.stat().st_size
                checks["events_bytes"] = size
                if size <= 0:
                    set_fail("events_jsonl_empty")
            except Exception:
                pass

            had_events_bom = False

            if exit_code == RC_OK and args.events_parse == "YES":
                parsed = 0
                bad = 0
                try:
                    with evs_path.open("r", encoding="utf-8") as f:
                        for i, line in enumerate(f):
                            if i >= int(args.max_events_lines):
                                break
                            line = line.rstrip("\n")
                            if i == 0 and line.startswith("\ufeff"):
                                had_events_bom = True
                                line = line.lstrip("\ufeff")
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
                    checks["events_jsonl_utf8_bom"] = bool(had_events_bom)

                    if had_events_bom and args.bom_strict == "YES":
                        set_fail("events_jsonl_has_utf8_bom")

                    if parsed == 0:
                        set_fail("events_jsonl_no_parsable_objects")
                    if bad > 0:
                        set_infra(f"events_jsonl_has_bad_lines: {bad}")

                except Exception as e:
                    set_infra(f"events_jsonl_read_error: {type(e).__name__}: {e}")

    # final_report_json declared path consistency (optional)
    if exit_code == RC_OK and isinstance(final, dict) and declared_final:
        df = _safe_resolve(Path(str(declared_final)))
        checks["final_report_json_declared"] = str(df)
        if df != _safe_resolve(final_path):
            set_fail(
                f"final_report_json_path_mismatch: declared={df} actual={_safe_resolve(final_path)}"
            )

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
