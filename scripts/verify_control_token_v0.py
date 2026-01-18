import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

NONCE_RE = re.compile(r"^[a-f0-9]{16,64}$")


def utc_ts() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def emit(
    ok: bool,
    exit_code: int,
    reason_code: str,
    token_path: str,
    run_id: str,
    job_type: str,
    detail: str = "",
) -> int:
    out = {
        "schema": "foundry_control_token_verify_v0",
        "ts_utc": utc_ts(),
        "ok": ok,
        "exit_code": exit_code,
        "reason_code": reason_code,
        "token_path": token_path,
        "expected": {"run_id": run_id, "job_type": job_type},
    }
    if detail:
        out["detail"] = detail
    print(json.dumps(out, ensure_ascii=False))
    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-TokenPath", required=True)
    ap.add_argument("-RunId", required=True)
    ap.add_argument("-JobType", required=True)
    args = ap.parse_args()

    token_path = args.TokenPath
    run_id = args.RunId
    job_type = args.JobType

    p = Path(token_path)
    if not p.exists():
        return emit(False, 1, "CONTROL_TOKEN.MISSING", token_path, run_id, job_type)

    try:
        tok = load_json(p)
    except Exception as e:
        return emit(
            False, 1, "CONTROL_TOKEN.BAD_JSON", token_path, run_id, job_type, repr(e)
        )

    if tok.get("schema") != "control_token_v0":
        return emit(
            False, 1, "CONTROL_TOKEN.SCHEMA_MISMATCH", token_path, run_id, job_type
        )

    if tok.get("run_id") != run_id:
        return emit(
            False, 1, "CONTROL_TOKEN.RUN_ID_MISMATCH", token_path, run_id, job_type
        )

    if tok.get("job_type") != job_type:
        return emit(
            False, 1, "CONTROL_TOKEN.JOB_TYPE_MISMATCH", token_path, run_id, job_type
        )

    nonce = tok.get("nonce")
    if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
        return emit(
            False, 1, "CONTROL_TOKEN.NONCE_INVALID", token_path, run_id, job_type
        )

    return emit(True, 0, "CONTROL_TOKEN.OK", token_path, run_id, job_type)


if __name__ == "__main__":
    raise SystemExit(main())
