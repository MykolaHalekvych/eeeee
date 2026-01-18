from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Err:
    field: str
    message: str


def _is_nonempty_str(x: Any) -> bool:
    return isinstance(x, str) and x.strip() != ""


def _is_rel_safe_path(p: str) -> Optional[str]:
    """
    Validate that path is relative and does not escape workspace:
    - no drive letters (C:\\...)
    - no UNC (\\\\server\\...)
    - no absolute (/ or \\ at start)
    - no '..' traversal segments
    """
    s = p.strip()
    if s == "":
        return "empty path"
    if s.startswith("\\\\"):
        return "UNC paths are not allowed"
    if len(s) >= 2 and s[1] == ":":
        return "absolute drive paths are not allowed"
    if s.startswith("/") or s.startswith("\\"):
        return "absolute paths are not allowed"

    norm = s.replace("\\", "/")
    parts = [x for x in norm.split("/") if x != ""]
    if any(seg == ".." for seg in parts):
        return "path traversal '..' is not allowed"
    return None


def _validate_allowed_paths(obj: Dict[str, Any], errs: List[Err]) -> None:
    allowed_paths = obj.get("allowed_paths")
    if not isinstance(allowed_paths, list) or len(allowed_paths) == 0:
        errs.append(Err(field="allowed_paths", message="must be a non-empty list[str]"))
        return

    for i, p in enumerate(allowed_paths):
        if not _is_nonempty_str(p):
            errs.append(
                Err(field=f"allowed_paths[{i}]", message="must be a non-empty string")
            )
            continue
        why = _is_rel_safe_path(str(p))
        if why:
            errs.append(Err(field=f"allowed_paths[{i}]", message=why))


def validate_job_request(obj: Any) -> List[Err]:
    errs: List[Err] = []
    if not isinstance(obj, dict):
        return [Err(field="$", message="job_request must be a JSON object")]

    schema = obj.get("schema")
    if schema not in ("job_request_v0", "job_request_v1"):
        return [
            Err(
                field="schema",
                message="schema must be 'job_request_v0' or 'job_request_v1'",
            )
        ]

    # Common: product_id
    product_id = obj.get("product_id")
    if not _is_nonempty_str(product_id):
        errs.append(Err(field="product_id", message="must be a non-empty string"))

    # Common: allowed_paths
    _validate_allowed_paths(obj, errs)

    # v0: requirements is a string
    if schema == "job_request_v0":
        requirements = obj.get("requirements")
        if not _is_nonempty_str(requirements):
            errs.append(Err(field="requirements", message="must be a non-empty string"))

    # v1: structured requirements + workspace
    if schema == "job_request_v1":
        # kit_id optional but if present must be non-empty string
        kit_id = obj.get("kit_id")
        if kit_id is not None and not _is_nonempty_str(kit_id):
            errs.append(
                Err(field="kit_id", message="must be a non-empty string if provided")
            )

        # workspace object
        workspace = obj.get("workspace")
        if workspace is None or not isinstance(workspace, dict):
            errs.append(Err(field="workspace", message="must be an object"))
        else:
            entrypoint = workspace.get("entrypoint")
            if not _is_nonempty_str(entrypoint):
                errs.append(
                    Err(
                        field="workspace.entrypoint",
                        message="must be a non-empty string",
                    )
                )
            else:
                why = _is_rel_safe_path(str(entrypoint))
                if why:
                    errs.append(Err(field="workspace.entrypoint", message=why))

            cli_entry = workspace.get("cli_entry")
            if cli_entry is not None:
                if not _is_nonempty_str(cli_entry):
                    errs.append(
                        Err(
                            field="workspace.cli_entry",
                            message="must be a non-empty string if provided",
                        )
                    )
                else:
                    why = _is_rel_safe_path(str(cli_entry))
                    if why:
                        errs.append(Err(field="workspace.cli_entry", message=why))

            stdlib_only = workspace.get("stdlib_only")
            if stdlib_only is not None and not isinstance(stdlib_only, bool):
                errs.append(
                    Err(
                        field="workspace.stdlib_only",
                        message="must be boolean if provided",
                    )
                )

        # requirements object with summary
        req = obj.get("requirements")
        if req is None or not isinstance(req, dict):
            errs.append(Err(field="requirements", message="must be an object"))
        else:
            summary = req.get("summary")
            if not _is_nonempty_str(summary):
                errs.append(
                    Err(
                        field="requirements.summary",
                        message="must be a non-empty string",
                    )
                )

    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-request", required=True, help="Path to job_request.json")
    ap.add_argument(
        "--out", required=False, help="Optional path to write validation report JSON"
    )
    args = ap.parse_args()

    jr_path = Path(args.job_request)

    report: Dict[str, Any] = {
        "schema": "job_request_validation_v1",
        "ok": False,
        "exit_code": 2,
        "job_request_path": str(jr_path),
        "errors": [],
    }

    try:
        raw = jr_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        report["errors"] = [{"field": "job_request_path", "message": "file_not_found"}]
        report["exit_code"] = 2
    except Exception as e:
        report["errors"] = [
            {
                "field": "job_request_path",
                "message": f"io_error: {type(e).__name__}: {e}",
            }
        ]
        report["exit_code"] = 2
    else:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            report["errors"] = [
                {
                    "field": "$",
                    "message": f"json_decode_error: {e.msg} (line {e.lineno}, col {e.colno})",
                }
            ]
            report["exit_code"] = 1
        else:
            errs = validate_job_request(obj)
            if errs:
                report["errors"] = [
                    {"field": e.field, "message": e.message} for e in errs
                ]
                report["exit_code"] = 1
                report["ok"] = False
            else:
                report["ok"] = True
                report["exit_code"] = 0
                report["errors"] = []

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(json.dumps(report, ensure_ascii=False))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
