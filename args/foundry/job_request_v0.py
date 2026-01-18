from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "job_request_v0"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail(msg: str, code: int = 1) -> int:
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "ok": False,
                "exit_code": code,
                "ts_utc": utc_ts(),
                "error": msg,
            },
            ensure_ascii=False,
        )
    )
    return code


def validate(data: dict[str, Any]) -> tuple[bool, str | None]:
    if data.get("schema") != SCHEMA:
        return False, "schema must be job_request_v0"

    pid = data.get("product_id")
    if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9_\-]{3,80}", pid):
        return False, "product_id must match [a-z0-9_\-]{3,80}"

    req = data.get("requirements")
    if not isinstance(req, str) or len(req.strip()) < 10:
        return False, "requirements must be a non-empty string (>=10 chars)"

    # optional: allowed file edits (whitelist)
    allowed = data.get("allowed_paths")
    if allowed is not None:
        if not isinstance(allowed, list) or not all(
            isinstance(x, str) for x in allowed
        ):
            return False, "allowed_paths must be an array of strings"

    return True, None


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate job_request_v0 JSON")
    ap.add_argument("--in", dest="inp", required=True)
    args = ap.parse_args()

    p = Path(args.inp)
    if not p.exists():
        return fail(f"input not found: {p}", 2)

    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:  # noqa: BLE001
        return fail(f"invalid json: {e}", 1)

    ok, err = validate(data)
    if not ok:
        return fail(err or "invalid job_request", 1)

    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "ok": True,
                "exit_code": 0,
                "ts_utc": utc_ts(),
                "product_id": data["product_id"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
