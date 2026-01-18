from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass
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


class GateError(Exception):
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


@dataclass(frozen=True)
class Constraints:
    max_patch_files: int
    max_total_chars: int
    max_string_chars: int


def _norm_path_for_compare(p: str) -> str:
    p2 = p.replace("\\", "/").strip()
    while p2.startswith("./"):
        p2 = p2[2:]
    return p2.lower()


def _is_safe_relpath(p: str) -> bool:
    p2 = p.replace("\\", "/").strip()
    if p2.startswith("/") or p2.startswith("\\"):
        return False
    if ":" in p2:
        return False
    parts = [x for x in p2.split("/") if x not in ("", ".")]
    return ".." not in parts


def _require_keys(obj: Any, required: set[str], optional: set[str], where: str) -> None:
    if not isinstance(obj, dict):
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", f"{where}:not_object")
    keys = set(obj.keys())
    missing = required - keys
    if missing:
        raise GateError(
            1,
            "FAIL_CONTRACT_MISSING_FIELD",
            f"{where}:missing:{','.join(sorted(missing))}",
        )
    unknown = keys - required - optional
    if unknown:
        raise GateError(
            1,
            "FAIL_CONTRACT_UNKNOWN_FIELD",
            f"{where}:unknown:{','.join(sorted(unknown))}",
        )


def _read_json_file(path: Path, max_bytes: int, where: str) -> Any:
    try:
        data = path.read_bytes()
    except FileNotFoundError as e:
        raise GateError(
            2, "INFRA_MISSING_INPUT", f"{where}:file_not_found", {"path": str(path)}
        ) from e
    except Exception as e:
        raise GateError(
            2,
            "INFRA_IO_ERROR",
            f"{where}:read_error:{type(e).__name__}",
            {"path": str(path)},
        ) from e

    if len(data) > max_bytes:
        raise GateError(
            1,
            "FAIL_OVERSIZED_INPUT",
            f"{where}:bytes:{len(data)}",
            {"path": str(path), "max_bytes": max_bytes},
        )

    try:
        return json.loads(data.decode("utf-8-sig"))
    except Exception as e:
        raise GateError(
            1,
            "FAIL_CONTRACT_JSON_INVALID",
            f"{where}:json_parse_error:{type(e).__name__}",
            {"path": str(path)},
        ) from e


def _read_text_ref(path: Path, where: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise GateError(
            2, "INFRA_MISSING_INPUT", f"{where}:file_not_found", {"path": str(path)}
        ) from e
    except Exception as e:
        raise GateError(
            2,
            "INFRA_IO_ERROR",
            f"{where}:read_error:{type(e).__name__}",
            {"path": str(path)},
        ) from e


def _sum_and_check_str(s: str, name: str, c: Constraints, total: int) -> int:
    if len(s) > c.max_string_chars:
        raise GateError(
            1,
            "FAIL_OVERSIZED_TEXT",
            f"{name}:len:{len(s)}",
            {"max_string_chars": c.max_string_chars},
        )
    return total + len(s)


def _validate_job_request(
    req: Any, repo_root: Path
) -> tuple[str, Constraints, list[str], Path, Path]:
    _require_keys(
        req,
        required={
            "schema",
            "inputs",
            "allowed_paths_ref",
            "prompt_ref",
            "constraints",
            "run_id",
        },
        optional=set(),
        where="job_request",
    )
    if req["schema"] != "llm_job_request_v0":
        raise GateError(1, "FAIL_CONTRACT_SCHEMA_MISMATCH", "job_request:schema")
    if not isinstance(req["run_id"], str) or not req["run_id"].strip():
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "job_request:run_id")

    _require_keys(
        req["inputs"],
        required={"task", "context_ref"},
        optional=set(),
        where="job_request.inputs",
    )
    if not isinstance(req["inputs"]["task"], str):
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "job_request.inputs:task")
    if not isinstance(req["inputs"]["context_ref"], str):
        raise GateError(
            1, "FAIL_CONTRACT_TYPE_INVALID", "job_request.inputs:context_ref"
        )

    _require_keys(
        req["constraints"],
        required={
            "max_patch_files",
            "max_total_chars",
            "max_string_chars",
            "forbid_network",
            "forbid_execute",
        },
        optional=set(),
        where="job_request.constraints",
    )
    c_obj = req["constraints"]
    if not isinstance(c_obj["max_patch_files"], int) or c_obj["max_patch_files"] < 0:
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "constraints:max_patch_files")
    if not isinstance(c_obj["max_total_chars"], int) or c_obj["max_total_chars"] <= 0:
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "constraints:max_total_chars")
    if not isinstance(c_obj["max_string_chars"], int) or c_obj["max_string_chars"] <= 0:
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "constraints:max_string_chars")
    if not isinstance(c_obj["forbid_network"], bool) or not isinstance(
        c_obj["forbid_execute"], bool
    ):
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "constraints:forbid_*")

    constraints = Constraints(
        max_patch_files=int(c_obj["max_patch_files"]),
        max_total_chars=int(c_obj["max_total_chars"]),
        max_string_chars=int(c_obj["max_string_chars"]),
    )

    if (
        not isinstance(req["allowed_paths_ref"], str)
        or not req["allowed_paths_ref"].strip()
    ):
        raise GateError(
            1, "FAIL_CONTRACT_MISSING_FIELD", "job_request:allowed_paths_ref"
        )
    if not isinstance(req["prompt_ref"], str) or not req["prompt_ref"].strip():
        raise GateError(1, "FAIL_CONTRACT_MISSING_FIELD", "job_request:prompt_ref")

    allowed_paths_ref = repo_root / Path(req["allowed_paths_ref"])
    prompt_ref = repo_root / Path(req["prompt_ref"])

    allowed_ref_obj = _read_json_file(
        allowed_paths_ref, max_bytes=200_000, where="allowed_paths_ref"
    )
    _require_keys(
        allowed_ref_obj,
        required={"schema", "allowed_paths"},
        optional=set(),
        where="allowed_paths_ref",
    )
    if allowed_ref_obj["schema"] != "allowed_paths_ref_v0":
        raise GateError(1, "FAIL_CONTRACT_SCHEMA_MISMATCH", "allowed_paths_ref:schema")
    if (
        not isinstance(allowed_ref_obj["allowed_paths"], list)
        or not allowed_ref_obj["allowed_paths"]
    ):
        raise GateError(
            1, "FAIL_CONTRACT_TYPE_INVALID", "allowed_paths_ref:allowed_paths"
        )

    allowed_paths: list[str] = []
    for i, p in enumerate(allowed_ref_obj["allowed_paths"]):
        if not isinstance(p, str) or not p.strip():
            raise GateError(
                1, "FAIL_CONTRACT_TYPE_INVALID", f"allowed_paths_ref:allowed_paths[{i}]"
            )
        if not _is_safe_relpath(p):
            raise GateError(
                1,
                "FAIL_ALLOWLIST_VIOLATION",
                "allowed_paths_ref:unsafe_path",
                {"path": p},
            )
        allowed_paths.append(_norm_path_for_compare(p))

    _read_text_ref(prompt_ref, where="prompt_ref")

    return str(req["run_id"]), constraints, allowed_paths, allowed_paths_ref, prompt_ref


def _validate_job_result(
    res: Any, c: Constraints, allowed_paths: list[str]
) -> dict[str, Any]:
    _require_keys(
        res,
        required={
            "schema",
            "ok",
            "proposals",
            "patch_payload",
            "diagnostics",
            "model_id",
        },
        optional=set(),
        where="job_result",
    )
    if res["schema"] != "llm_job_result_v0":
        raise GateError(1, "FAIL_CONTRACT_SCHEMA_MISMATCH", "job_result:schema")
    if not isinstance(res["ok"], bool):
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "job_result:ok")
    if not isinstance(res["model_id"], str) or not res["model_id"].strip():
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "job_result:model_id")

    _require_keys(
        res["diagnostics"],
        required={"tokens_in", "tokens_out", "notes"},
        optional=set(),
        where="job_result.diagnostics",
    )
    d = res["diagnostics"]
    if not isinstance(d["tokens_in"], int) or not isinstance(d["tokens_out"], int):
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "diagnostics:tokens")
    if not isinstance(d["notes"], str):
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "diagnostics:notes")

    if not isinstance(res["proposals"], list):
        raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "job_result:proposals")

    total_chars = 0
    total_chars = _sum_and_check_str(str(res["model_id"]), "model_id", c, total_chars)
    total_chars = _sum_and_check_str(
        str(d["notes"]), "diagnostics.notes", c, total_chars
    )

    for i, p in enumerate(res["proposals"]):
        _require_keys(
            p, required={"kind", "text"}, optional=set(), where=f"proposal[{i}]"
        )
        if p["kind"] not in ("TEXT", "PATCH"):
            raise GateError(1, "FAIL_CONTRACT_VALUE_INVALID", f"proposal[{i}]:kind")
        if not isinstance(p["text"], str):
            raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", f"proposal[{i}]:text")
        total_chars = _sum_and_check_str(
            p["text"], f"proposal[{i}].text", c, total_chars
        )

    patch = res["patch_payload"]
    patch_info: dict[str, Any] = {"has_patch": False, "patch_files": 0}
    if patch is None:
        pass
    else:
        _require_keys(
            patch, required={"format", "changes"}, optional=set(), where="patch_payload"
        )
        if patch["format"] != "patch_payload_v0":
            raise GateError(1, "FAIL_CONTRACT_SCHEMA_MISMATCH", "patch_payload:format")
        if not isinstance(patch["changes"], list):
            raise GateError(1, "FAIL_CONTRACT_TYPE_INVALID", "patch_payload:changes")
        if len(patch["changes"]) > c.max_patch_files:
            raise GateError(
                1,
                "FAIL_CONTRACT_VALUE_INVALID",
                "patch_payload:too_many_files",
                {"max_patch_files": c.max_patch_files},
            )

        patch_info["has_patch"] = True
        patch_info["patch_files"] = len(patch["changes"])

        for i, ch in enumerate(patch["changes"]):
            _require_keys(
                ch,
                required={"path", "op"},
                optional={"content"},
                where=f"patch_change[{i}]",
            )
            if not isinstance(ch["path"], str) or not ch["path"].strip():
                raise GateError(
                    1, "FAIL_CONTRACT_TYPE_INVALID", f"patch_change[{i}]:path"
                )
            if not isinstance(ch["op"], str):
                raise GateError(
                    1, "FAIL_CONTRACT_TYPE_INVALID", f"patch_change[{i}]:op"
                )
            if ch["op"] not in ("write", "delete"):
                raise GateError(
                    1, "FAIL_CONTRACT_VALUE_INVALID", f"patch_change[{i}]:op"
                )

            if not _is_safe_relpath(ch["path"]):
                raise GateError(
                    1,
                    "FAIL_ALLOWLIST_VIOLATION",
                    f"patch_change[{i}]:unsafe_path",
                    {"path": ch["path"]},
                )

            p_norm = _norm_path_for_compare(ch["path"])
            if p_norm not in allowed_paths:
                raise GateError(
                    1,
                    "FAIL_ALLOWLIST_VIOLATION",
                    f"patch_change[{i}]:path_not_allowed",
                    {"path": ch["path"]},
                )

            total_chars = _sum_and_check_str(
                ch["path"], f"patch_change[{i}].path", c, total_chars
            )

            if ch["op"] == "write":
                if "content" not in ch or not isinstance(ch["content"], str):
                    raise GateError(
                        1,
                        "FAIL_CONTRACT_MISSING_FIELD",
                        f"patch_change[{i}]:content_required",
                    )
                total_chars = _sum_and_check_str(
                    ch["content"], f"patch_change[{i}].content", c, total_chars
                )

    if total_chars > c.max_total_chars:
        raise GateError(
            1,
            "FAIL_OVERSIZED_TEXT",
            "total_chars_exceeded",
            {"total_chars": total_chars, "max_total_chars": c.max_total_chars},
        )

    return {"total_chars": total_chars, **patch_info}


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--JobRequest", required=True)
    ap.add_argument("--JobResult", required=True)
    ap.add_argument("--OutDir", required=True)
    args = ap.parse_args()

    ts = _ts_utc_iso()
    out_dir = Path(args.OutDir)
    step_dir = out_dir / "step_00_validate"
    stdout_path = step_dir / "stdout.json"
    stderr_path = step_dir / "stderr.txt"
    summary_path = out_dir / "summary.json"

    repo_root = Path.cwd()

    err_text = ""
    run_id = "UNKNOWN_RUN_ID"
    exit_code = 2
    reason_code = "INFRA_UNKNOWN"
    child_reason_code = "INFRA_UNKNOWN"

    details: dict[str, Any] = {}

    try:
        req = _read_json_file(
            Path(args.JobRequest), max_bytes=500_000, where="job_request_file"
        )
        res = _read_json_file(
            Path(args.JobResult), max_bytes=5_000_000, where="job_result_file"
        )

        run_id, c, allowed_paths, allowed_paths_ref, prompt_ref = _validate_job_request(
            req, repo_root
        )
        res_info = _validate_job_result(res, c, allowed_paths)

        exit_code = 0
        reason_code = "OK"
        child_reason_code = "OK"
        details = {
            "allowed_paths_ref": str(allowed_paths_ref),
            "prompt_ref": str(prompt_ref),
            "result_info": res_info,
        }

    except GateError as ge:
        exit_code = ge.exit_code
        reason_code = ge.reason_code
        child_reason_code = ge.child_reason_code
        details = ge.details
        err_text = _json_compact(
            {"error": reason_code, "child": child_reason_code, "details": details}
        )
    except Exception as e:
        exit_code = 2
        reason_code = "INFRA_EXCEPTION"
        child_reason_code = f"INFRA_EXCEPTION:{type(e).__name__}"
        err_text = traceback.format_exc()

    out = {
        "schema": "validate_llm_proposal_v0",
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
