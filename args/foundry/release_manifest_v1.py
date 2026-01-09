from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2


def utc_now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json_utf8sig(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json_no_bom(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def maybe_file_entry(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"path": path.name, "present": False}
    return {"path": path.name, "present": True, "sha256": sha256_file(path)}


def derive_product_id_from_final_report(run_dir: Path) -> Tuple[str, List[str]]:
    fr = run_dir / "final_report.json"
    if not fr.exists():
        return "", []
    try:
        j = read_json_utf8sig(fr)
        pid = str(j.get("product_id") or "").strip()
        return pid, []
    except Exception as e:
        return "", [f"final_report_parse_error:{type(e).__name__}:{e}"]


def build_release_manifest_v1(
    run_dir: Optional[Path],
    dist_dir: Path,
    release_id: str,
    product_id: str = "",
) -> Tuple[Dict[str, Any], List[str]]:
    errors: List[str] = []

    pid = product_id.strip()
    run_section: Dict[str, Any] = {
        "run_id": "",
        "run_dir": "",
        "final_report_json": "",
        "events_jsonl": "",
        "evidence_dir": "",
    }

    if run_dir is not None:
        run_section["run_id"] = run_dir.name
        run_section["run_dir"] = str(run_dir)
        run_section["final_report_json"] = str(run_dir / "final_report.json")
        run_section["events_jsonl"] = str(run_dir / "events.jsonl")
        run_section["evidence_dir"] = str(run_dir / "evidence")

        # product_id best-effort from final_report if not provided
        if not pid:
            dpid, derr = derive_product_id_from_final_report(run_dir)
            pid = dpid or pid
            errors.extend(derr)

    if not pid:
        errors.append("product_id_missing")

    # dist files (product gate will enforce strict presence)
    app_exe = dist_dir / "app.exe"
    hashes_json = dist_dir / "hashes.json"
    acceptance_gate = dist_dir / "acceptance_gate.json"
    runbook_md = dist_dir / "runbook.md"
    evidence_md = dist_dir / "evidence.md"
    config_example = dist_dir / "config.example.json"

    manifest: Dict[str, Any] = {
        "schema": "release_manifest_v1",
        "version": 1,
        "ts_utc": utc_now_iso(),
        "product_id": pid,
        "release_id": release_id,
        "run": run_section,
        "bundle": {
            "dist_dir": str(dist_dir),
            "release_zip_name": f"{release_id}.zip",
            "files": {
                "app.exe": maybe_file_entry(app_exe),
                "hashes.json": maybe_file_entry(hashes_json),
                "acceptance_gate.json": maybe_file_entry(acceptance_gate),
                "runbook.md": maybe_file_entry(runbook_md),
                "evidence.md": maybe_file_entry(evidence_md),
                "config.example.json": maybe_file_entry(config_example),
                "release_manifest_v1.json": {"path": "release_manifest_v1.json", "present": True},
            },
        },
        "errors": errors,
    }
    return manifest, errors


def emit(obj: Dict[str, Any], code: int) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--dist-dir", required=True)
    ap.add_argument("--release-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--product-id", default="")
    args = ap.parse_args()

    ts = utc_now_iso()
    try:
        run_dir = Path(args.run_dir).resolve() if args.run_dir.strip() else None
        dist_dir = Path(args.dist_dir).resolve()
        out = Path(args.out).resolve()

        manifest, errors = build_release_manifest_v1(
            run_dir=run_dir,
            dist_dir=dist_dir,
            release_id=str(args.release_id),
            product_id=str(args.product_id),
        )
        write_json_no_bom(out, manifest)

        code = RC_OK if len(errors) == 0 else RC_FAIL
        emit(
            {
                "schema": "release_manifest_build_v1",
                "ts_utc": ts,
                "ok": (code == 0),
                "exit_code": code,
                "out": str(out),
                "run_dir": str(run_dir) if run_dir else "",
                "dist_dir": str(dist_dir),
                "release_id": str(args.release_id),
                "errors": errors,
            },
            code,
        )
    except Exception as e:
        emit(
            {
                "schema": "release_manifest_build_v1",
                "ts_utc": ts,
                "ok": False,
                "exit_code": RC_INFRA,
                "error": {"kind": "infra", "type": type(e).__name__, "message": str(e)},
            },
            RC_INFRA,
        )


if __name__ == "__main__":
    main()