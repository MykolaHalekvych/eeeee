from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any


def _ts_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class StubError(Exception):
    def __init__(
        self,
        exit_code: int,
        reason_code: str,
        child_reason_code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.exit_code = int(exit_code)
        self.reason_code = reason_code
        self.child_reason_code = child_reason_code or "ERR_CHILD_CODE_MISSING"
        self.details = details or {}


def _read_json(path: Path, max_bytes: int, where: str) -> Any:
    try:
        b = path.read_bytes()
    except FileNotFoundError as e:
        raise StubError(
            2, "INFRA_MISSING_INPUT", f"{where}:file_not_found", {"path": str(path)}
        ) from e
    except Exception as e:
        raise StubError(
            2,
            "INFRA_IO_ERROR",
            f"{where}:read_error:{type(e).__name__}",
            {"path": str(path)},
        ) from e

    if len(b) > max_bytes:
        raise StubError(
            1,
            "FAIL_OVERSIZED_INPUT",
            f"{where}:bytes:{len(b)}",
            {"path": str(path), "max_bytes": max_bytes},
        )

    try:
        return json.loads(b.decode("utf-8-sig"))
    except Exception as e:
        raise StubError(
            1,
            "FAIL_CONTRACT_JSON_INVALID",
            f"{where}:json_parse_error:{type(e).__name__}",
            {"path": str(path)},
        ) from e


def _norm_path_for_compare(p: str) -> str:
    p2 = p.replace("\\", "/").strip()
    while p2.startswith("./"):
        p2 = p2[2:]
    return p2.lower()


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--JobRequest", required=True)
    ap.add_argument("--OutResult", required=True)
    ap.add_argument("--OutDir", required=True)
    args = ap.parse_args()

    ts = _ts_utc_iso()
    repo_root = Path.cwd()
    out_dir = Path(args.OutDir)
    step_dir = out_dir / "step_00_stub"
    stdout_path = step_dir / "stdout.json"
    stderr_path = step_dir / "stderr.txt"
    summary_path = out_dir / "summary.json"

    exit_code = 2
    reason_code = "INFRA_UNKNOWN"
    child_reason_code = "INFRA_UNKNOWN"
    err_text = ""
    run_id = "UNKNOWN_RUN_ID"
    details: dict[str, Any] = {}

    try:
        req = _read_json(
            Path(args.JobRequest), max_bytes=500_000, where="job_request_file"
        )

        run_id = str(req.get("run_id", "UNKNOWN_RUN_ID"))
        allowed_paths_ref = req.get("allowed_paths_ref")
        if not isinstance(allowed_paths_ref, str) or not allowed_paths_ref.strip():
            raise StubError(
                1, "FAIL_CONTRACT_MISSING_FIELD", "job_request:allowed_paths_ref"
            )

        allowed_ref = _read_json(
            repo_root / Path(allowed_paths_ref),
            max_bytes=200_000,
            where="allowed_paths_ref",
        )
        allowed_paths = allowed_ref.get("allowed_paths")
        if not isinstance(allowed_paths, list) or not allowed_paths:
            raise StubError(
                1, "FAIL_CONTRACT_TYPE_INVALID", "allowed_paths_ref:allowed_paths"
            )

        target = allowed_paths[0]
        if not isinstance(target, str) or not target.strip():
            raise StubError(
                1, "FAIL_CONTRACT_TYPE_INVALID", "allowed_paths_ref:allowed_paths[0]"
            )

        # Deterministic content
        patch_line = f"STUB_PATCH|{run_id}\n"
        result = {
            "schema": "llm_job_result_v0",
            "ok": True,
            "proposals": [
                {
                    "kind": "TEXT",
                    "text": "OFFLINE stub v0: deterministic proposal produced. No network. No writes.",
                }
            ],
            "patch_payload": {
                "format": "patch_payload_v0",
                "changes": [
                    {
                        "path": _norm_path_for_compare(str(target)).replace("/", "/"),
                        "op": "write",
                        "content": patch_line,
                    }
                ],
            },
            "diagnostics": {
                "tokens_in": 0,
                "tokens_out": 0,
                "notes": "offline_stub_v0",
            },
            "model_id": "stub_v0",
        }

        out_path = Path(args.OutResult)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(out_path, result)

        exit_code = 0
        reason_code = "OK"
        child_reason_code = "OK"
        details = {"out_result": str(out_path), "target_path": str(target)}

    except StubError as se:
        exit_code = se.exit_code
        reason_code = se.reason_code
        child_reason_code = se.child_reason_code
        details = se.details
        err_text = _json_compact(
            {"error": reason_code, "child": child_reason_code, "details": details}
        )
    except Exception as e:
        exit_code = 2
        reason_code = "INFRA_EXCEPTION"
        child_reason_code = f"INFRA_EXCEPTION:{type(e).__name__}"
        err_text = traceback.format_exc()

    out = {
        "schema": "llm_stub_v0",
        "ts_utc": ts,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "reason_code": reason_code,
        "child_reason_code": child_reason_code or "ERR_CHILD_CODE_MISSING",
        "repo": str(repo_root),
        "run_id": run_id,
        "out_dir": str(out_dir),
        "details": details,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(summary_path, out)
    _write_json(stdout_path, out)
    _write_text(stderr_path, err_text)

    sys.stdout.write(_json_compact(out) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
