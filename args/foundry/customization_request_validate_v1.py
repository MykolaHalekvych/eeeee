from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2

BANNED_TARGETS = {
    "app.exe",
    "hashes.json",
    "acceptance_gate.json",
    "release_manifest_v1.json",
}

ALLOWED_MODES = {"OVERLAY_BUNDLE", "CONFIG_ONLY"}


def utc_now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(obj: Dict[str, Any], code: int) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    sys.exit(code)


def read_json_utf8sig(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def norm_rel_path(p: str) -> str:
    p = (p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def is_safe_rel(p: str) -> bool:
    if not p:
        return False
    if p.startswith("/") or p.startswith("\\"):
        return False
    parts = [x for x in p.split("/") if x]
    if any(x == ".." for x in parts):
        return False
    if ":" in p:  # blocks drive letters in "C:\..."
        return False
    return True


def validate_request(j: Dict[str, Any]) -> List[str]:
    errs: List[str] = []

    if str(j.get("schema") or "") != "customization_request_v1":
        errs.append("schema_invalid")
    if int(j.get("version") or 0) != 1:
        errs.append("version_invalid")

    mode = str(j.get("mode") or "").strip()
    if mode not in ALLOWED_MODES:
        errs.append("mode_invalid")

    product_id = str(j.get("product_id") or "").strip()
    if not product_id:
        errs.append("product_id_missing")

    base_release_id = str(j.get("base_release_id") or "").strip()
    if not base_release_id:
        errs.append("base_release_id_missing")

    overlay_dir = str(j.get("overlay_dir") or "").strip()
    if not overlay_dir:
        errs.append("overlay_dir_missing")
    else:
        # allow relative path only in v1 (keeps it safe & reproducible)
        od = norm_rel_path(overlay_dir)
        if not is_safe_rel(od):
            errs.append("overlay_dir_unsafe")

    allowlist = j.get("allowlist")
    if not isinstance(allowlist, list) or len(allowlist) == 0:
        errs.append("allowlist_missing_or_empty")
    else:
        for raw in allowlist:
            if not isinstance(raw, str):
                errs.append("allowlist_item_not_string")
                continue
            p = norm_rel_path(raw)
            if not is_safe_rel(p):
                errs.append("allowlist_item_unsafe:" + p)
                continue
            if p in BANNED_TARGETS:
                errs.append("allowlist_banned_target:" + p)

    return errs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    ts = utc_now_iso()
    try:
        req_path = Path(args.request).resolve()
        j = read_json_utf8sig(req_path)
        if not isinstance(j, dict):
            emit(
                {
                    "schema": "customization_request_validate_v1",
                    "ts_utc": ts,
                    "ok": False,
                    "exit_code": RC_FAIL,
                    "errors": ["request_not_object"],
                },
                RC_FAIL,
            )

        errors = validate_request(j)

        # normalize for downstream
        norm = {
            "schema": "customization_request_v1",
            "version": 1,
            "request_id": str(j.get("request_id") or ""),
            "ts_utc": str(j.get("ts_utc") or ""),
            "mode": str(j.get("mode") or "").strip(),
            "product_id": str(j.get("product_id") or "").strip(),
            "base_release_id": str(j.get("base_release_id") or "").strip(),
            "overlay_dir": norm_rel_path(str(j.get("overlay_dir") or "")),
            "allowlist": [
                norm_rel_path(str(x))
                for x in (j.get("allowlist") or [])
                if isinstance(x, str)
            ],
        }

        outp = args.out.strip()
        if outp:
            op = Path(outp).resolve()
            op.parent.mkdir(parents=True, exist_ok=True)
            op.write_text(
                json.dumps(norm, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

        code = RC_OK if len(errors) == 0 else RC_FAIL
        emit(
            {
                "schema": "customization_request_validate_v1",
                "ts_utc": ts,
                "ok": (code == 0),
                "exit_code": code,
                "request_path": str(req_path),
                "errors": errors,
                "normalized": norm,
                "out": outp,
            },
            code,
        )

    except Exception as e:
        emit(
            {
                "schema": "customization_request_validate_v1",
                "ts_utc": ts,
                "ok": False,
                "exit_code": RC_INFRA,
                "error": {"kind": "infra", "type": type(e).__name__, "message": str(e)},
            },
            RC_INFRA,
        )


if __name__ == "__main__":
    main()
