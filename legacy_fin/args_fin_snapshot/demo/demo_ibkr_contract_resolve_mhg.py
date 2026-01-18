from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict

from args.ibkr.ibkr_contract_resolver_v1 import (
    IbkrConn,
    load_ibkr_connection,
    resolve_copper_contract_unattended,
)

SCHEMA = "demo_ibkr_contract_resolve_mhg_v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    # Support both attribute-style and dict-style configs
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _stdout_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    ts = _utc_now_iso()

    try:
        base = load_ibkr_connection()

        conn = IbkrConn(
            host=str(_get(base, "host")),
            port=int(_get(base, "port")),
            client_id=int(_get(base, "client_id")),
            timeout_s=float(_get(base, "timeout_s", 8.0)),
            wait_s=float(_get(base, "wait_s", 25.0)),
        )

        max_attempts = 3
        backoff_s = 1.0

        last_err: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                res: Dict[str, Any] = resolve_copper_contract_unattended(
                    symbol="MHG",
                    conn=conn,
                    sec_type="FUT",
                    exchange="COMEX",
                    currency="USD",
                )

                # Keep success output shape identical, but add minimal traceability
                if isinstance(res, dict):
                    res.setdefault("meta", {})
                    if isinstance(res["meta"], dict):
                        res["meta"]["attempt"] = attempt
                        res["meta"]["ts_utc"] = ts
                        res["meta"]["schema"] = SCHEMA

                _stdout_json(res)
                return 0 if bool(res.get("ok")) else 2

            except RuntimeError as e:
                msg = str(e)
                last_err = e

                # Retry ONLY for intermittent IBKR handshake nextValidId timeout
                if ("nextValidId" in msg) and (attempt < max_attempts):
                    time.sleep(backoff_s + attempt)  # 2s, 3s
                    continue

                # Non-retry RuntimeError or retries exhausted
                raise

        # Should not reach here, but keep deterministic
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 1,
            "severity": "WARN",
            "error": f"RuntimeError: {last_err}"
            if last_err
            else "RuntimeError: unknown",
            "traceback": None,
            "notes": ["exhausted_retries_nextValidId"],
        }
        _stdout_json(out)
        return 1

    except RuntimeError as e:
        msg = str(e)
        # nextValidId timeout after retries -> WARN (exit 1)
        if "nextValidId" in msg:
            out = {
                "schema": SCHEMA,
                "ts_utc": ts,
                "ok": False,
                "exit_code": 1,
                "severity": "WARN",
                "error": f"RuntimeError: {msg}",
                "traceback": None,
                "notes": ["ibkr_handshake_nextValidId_timeout"],
            }
            _stdout_json(out)
            return 1

        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "severity": "FAIL",
            "error": f"RuntimeError: {msg}",
            "traceback": traceback.format_exc(),
        }
        _stdout_json(out)
        return 2

    except Exception as e:
        out = {
            "schema": SCHEMA,
            "ts_utc": ts,
            "ok": False,
            "exit_code": 2,
            "severity": "FAIL",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }
        _stdout_json(out)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
