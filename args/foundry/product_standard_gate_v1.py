from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Set

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2

REQUIRED_FILES: Set[str] = {
    "app.exe",
    "hashes.json",
    "acceptance_gate.json",
    "runbook.md",
    "evidence.md",
    "config.example.json",
    "release_manifest_v1.json",
}


def utc_now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(obj: Dict[str, Any], code: int) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    sys.exit(code)


def read_json_bytes(b: bytes) -> Any:
    # tolerate UTF-8 BOM
    if b.startswith(b"\xef\xbb\xbf"):
        b = b[3:]
    return json.loads(b.decode("utf-8"))


def norm(n: str) -> str:
    n = n.replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    return n


def check_release_zip(release_zip: Path) -> Dict[str, Any]:
    if not release_zip.exists():
        return {
            "ok": False,
            "exit_code": RC_INFRA,
            "missing": sorted(REQUIRED_FILES),
            "errors": [f"release_zip_missing:{release_zip}"],
            "manifest": {"present": False, "ok": False, "schema": ""},
            "required": sorted(REQUIRED_FILES),
        }

    missing: List[str] = []
    errors: List[str] = []
    manifest_schema = ""
    manifest_ok = False
    manifest_present = False

    try:
        with zipfile.ZipFile(release_zip, "r") as z:
            names = {norm(n) for n in z.namelist() if not n.endswith("/")}
            for r in sorted(REQUIRED_FILES):
                if r not in names:
                    missing.append(r)

            if "release_manifest_v1.json" in names:
                manifest_present = True
                try:
                    mj = read_json_bytes(z.read("release_manifest_v1.json"))
                    manifest_schema = str(mj.get("schema") or "")
                    manifest_ok = manifest_schema == "release_manifest_v1"
                    if not manifest_ok:
                        errors.append(f"manifest_schema_invalid:{manifest_schema}")
                except Exception as e:
                    errors.append(f"manifest_parse_error:{type(e).__name__}:{e}")
            else:
                errors.append("manifest_missing_in_zip")

    except Exception as e:
        return {
            "ok": False,
            "exit_code": RC_INFRA,
            "missing": sorted(REQUIRED_FILES),
            "errors": [f"zip_open_error:{type(e).__name__}:{e}"],
            "manifest": {"present": False, "ok": False, "schema": ""},
            "required": sorted(REQUIRED_FILES),
        }

    ok = (len(missing) == 0) and (len(errors) == 0) and manifest_present and manifest_ok
    code = RC_OK if ok else RC_FAIL
    return {
        "ok": ok,
        "exit_code": code,
        "missing": missing,
        "errors": errors,
        "manifest": {
            "present": manifest_present,
            "ok": manifest_ok,
            "schema": manifest_schema,
        },
        "required": sorted(REQUIRED_FILES),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release-zip", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    ts = utc_now_iso()
    try:
        rz = Path(args.release_zip)
        rep = check_release_zip(rz)

        outp = args.out.strip()
        if outp:
            op = Path(outp)
            op.parent.mkdir(parents=True, exist_ok=True)
            op.write_text(
                json.dumps(rep, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

        emit(
            {
                "schema": "product_standard_gate_v1",
                "ts_utc": ts,
                "ok": bool(rep["ok"]),
                "exit_code": int(rep["exit_code"]),
                "release_zip": str(rz),
                "report": rep,
                "out": outp,
            },
            int(rep["exit_code"]),
        )
    except Exception as e:
        emit(
            {
                "schema": "product_standard_gate_v1",
                "ts_utc": ts,
                "ok": False,
                "exit_code": RC_INFRA,
                "error": {"kind": "infra", "type": type(e).__name__, "message": str(e)},
            },
            RC_INFRA,
        )


if __name__ == "__main__":
    main()
