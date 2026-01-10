from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2


def utc_now_iso() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write_text_no_bom(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json_no_bom(path: Path, obj: Any) -> None:
    write_text_no_bom(path, json_dumps(obj))


def append_event(events_path: Path, run_id: str, kind: str, data: Dict[str, Any]) -> None:
    ev = {
        "schema": "event_v0",
        "ts_utc": utc_now_iso(),
        "run_id": run_id,
        "step": "customize_release_v1",
        "kind": kind,
        "data": data,
    }
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8") as f:
        f.write(json_dumps(ev) + "\n")


def normalize_exit(code: int) -> int:
    return code if code in (0, 1, 2) else 2


def parse_one_json(raw: str) -> Optional[dict]:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # try last json-ish line
    lines = re.split(r"\r?\n", s)
    for i in range(len(lines) - 1, -1, -1):
        c = lines[i].strip()
        if c.startswith("{") and c.endswith("}"):
            try:
                obj = json.loads(c)
                return obj if isinstance(obj, dict) else None
            except Exception:
                continue
    return None


def classify(rc: int, parsed_ok: bool) -> Tuple[bool, int]:
    # infra if can't parse output
    if not parsed_ok:
        return True, 2
    return (rc == 2), rc


def run_py_module(
    repo: Path,
    evidence_dir: Path,
    step_id: str,
    module: str,
    args: List[str],
) -> Tuple[dict, bool, int]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    out_path = evidence_dir / f"{step_id}.stdout.txt"
    err_path = evidence_dir / f"{step_id}.stderr.txt"

    cmd = [sys.executable, "-m", module] + args
    p = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, check=False)

    out_path.write_text(p.stdout or "", encoding="utf-8")
    err_path.write_text(p.stderr or "", encoding="utf-8")

    rc = normalize_exit(int(p.returncode))
    obj = parse_one_json(p.stdout or "")
    parsed_ok = obj is not None
    infra, rc2 = classify(rc, parsed_ok)

    if not parsed_ok:
        obj = {"schema": "invalid_json", "raw_returncode": int(p.returncode)}

    return obj, infra, rc2


def sanitize_token(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "cust"
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", s)


def overlay_hash_from_report(rep: dict, base_release_id: str) -> str:
    applied = rep.get("applied") if isinstance(rep, dict) else None
    if not isinstance(applied, list):
        applied = []
    sig_lines: List[str] = []
    for a in applied:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        sha = str(a.get("sha256") or "")
        if name and sha:
            sig_lines.append(f"{name}={sha}")
    sig_lines.sort()
    if not sig_lines:
        sig_lines = ["no_applied"]
    blob = base_release_id + "\n" + "\n".join(sig_lines) + "\n"
    import hashlib

    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def derive_customer_release_id(product_id: str, base_release_id: str, request_id: str, overlay_hash: str) -> str:
    parts = base_release_id.split("__")
    prefix = product_id
    if len(parts) >= 2:
        prefix = f"{parts[0]}__{parts[1]}"
    short = overlay_hash[:10]
    return f"{prefix}__cust_{sanitize_token(request_id)}__{short}"


def emit_final_and_exit(final: dict, code: int) -> None:
    sys.stdout.write(json_dumps(final))
    sys.exit(int(code))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--request", required=True)
    ap.add_argument("--base-release-id-override", default="")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    req_path = Path(args.request)
    if not req_path.is_absolute():
        req_path = (repo / req_path).resolve()

    run_id = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "_" + os.urandom(4).hex()
    run_dir = repo / "args" / "data" / "runs" / run_id
    evidence_dir = run_dir / "evidence"
    events_jsonl = run_dir / "events.jsonl"
    final_json = run_dir / "final_report.json"

    run_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    write_text_no_bom(events_jsonl, "")

    append_event(events_jsonl, run_id, "start", {"repo": str(repo), "request": str(req_path), "base_release_override": str(args.base_release_id_override)})

    # ---- step 1: validate request ----
    req_norm = run_dir / "customization_request_normalized.json"
    v_obj, v_infra, v_rc = run_py_module(
        repo, evidence_dir, "validate_request",
        "args.foundry.customization_request_validate_v1",
        ["--request", str(req_path), "--out", str(req_norm)],
    )
    append_event(events_jsonl, run_id, "validate_done", {"rc": v_rc, "infra": v_infra, "out": str(req_norm)})

    if v_infra or v_rc != 0:
        final = {
            "schema": "factory_final_report_v1",
            "ok": False,
            "exit_code": (2 if (v_infra or v_rc == 2) else 1),
            "run_id": run_id,
            "product_id": "",
            "run_dir": str(run_dir),
            "entrypoint": "",
            "release_id": "",
            "release_zip": "",
            "release_hashes": "",
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_json),
            "acceptance_ok": False,
            "run_acceptance": "YES",
            "acceptance_report": "",
            "acceptance_embedded": "",
            "error": {"kind": ("infra" if (v_infra or v_rc == 2) else "fail"), "type": "validate_failed", "message": "customization request invalid"},
        }
        write_json_no_bom(final_json, final)
        emit_final_and_exit(final, int(final["exit_code"]))

    norm = v_obj.get("normalized") if isinstance(v_obj, dict) else None
    if not isinstance(norm, dict):
        norm = {}

    product_id = str(norm.get("product_id") or "").strip()
    base_rel = str(norm.get("base_release_id") or "").strip()
    request_id = str(norm.get("request_id") or "").strip()

    if args.base_release_id_override.strip():
        base_rel = args.base_release_id_override.strip()

    if not product_id or not base_rel:
        final = {
            "schema": "factory_final_report_v1",
            "ok": False,
            "exit_code": 1,
            "run_id": run_id,
            "product_id": product_id,
            "run_dir": str(run_dir),
            "entrypoint": "",
            "release_id": "",
            "release_zip": "",
            "release_hashes": "",
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_json),
            "acceptance_ok": False,
            "run_acceptance": "YES",
            "acceptance_report": "",
            "acceptance_embedded": "",
            "error": {"kind": "fail", "type": "missing_fields", "message": "product_id/base_release_id missing"},
        }
        write_json_no_bom(final_json, final)
        emit_final_and_exit(final, 1)

    # ---- step 2: verify base release ----
    vb_obj, vb_infra, vb_rc = run_py_module(
        repo, evidence_dir, "verify_base",
        "args.foundry.release_verify_v0",
        ["--release-id", base_rel],
    )
    append_event(events_jsonl, run_id, "verify_base_done", {"rc": vb_rc, "infra": vb_infra, "base_release_id": base_rel})
    if vb_infra or vb_rc != 0:
        code = 2 if (vb_infra or vb_rc == 2) else 1
        final = {
            "schema": "factory_final_report_v1",
            "ok": False,
            "exit_code": code,
            "run_id": run_id,
            "product_id": product_id,
            "run_dir": str(run_dir),
            "entrypoint": "",
            "release_id": "",
            "release_zip": "",
            "release_hashes": "",
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_json),
            "acceptance_ok": False,
            "run_acceptance": "YES",
            "acceptance_report": "",
            "acceptance_embedded": "",
            "error": {"kind": ("infra" if code == 2 else "fail"), "type": "base_release_verify_failed", "message": base_rel},
        }
        write_json_no_bom(final_json, final)
        emit_final_and_exit(final, code)

    # ---- step 3: unpack base release to workspace dist ----
    ws_root = repo / "args" / "data" / "workspaces" / run_id
    workspace_dist = ws_root / "dist"
    unpack_report = run_dir / "unpack_report.json"

    u_obj, u_infra, u_rc = run_py_module(
        repo, evidence_dir, "unpack_base",
        "args.foundry.release_unpack_v1",
        ["--release-id", base_rel, "--out-dir", str(workspace_dist), "--out-report", str(unpack_report)],
    )
    append_event(events_jsonl, run_id, "unpack_done", {"rc": u_rc, "infra": u_infra, "workspace_dist": str(workspace_dist), "report": str(unpack_report)})
    if u_infra or u_rc != 0:
        code = 2 if (u_infra or u_rc == 2) else 1
        final = {
            "schema": "factory_final_report_v1",
            "ok": False,
            "exit_code": code,
            "run_id": run_id,
            "product_id": product_id,
            "run_dir": str(run_dir),
            "entrypoint": "",
            "release_id": "",
            "release_zip": "",
            "release_hashes": "",
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_json),
            "acceptance_ok": False,
            "run_acceptance": "YES",
            "acceptance_report": "",
            "acceptance_embedded": "",
            "error": {"kind": ("infra" if code == 2 else "fail"), "type": "unpack_failed", "message": base_rel},
        }
        write_json_no_bom(final_json, final)
        emit_final_and_exit(final, code)

    # ---- step 4: apply overlay ----
    overlay_report = run_dir / "overlay_report.json"
    oa_obj, oa_infra, oa_rc = run_py_module(
        repo, evidence_dir, "apply_overlay",
        "args.foundry.overlay_apply_v1",
        ["--request", str(req_path), "--workspace-dir", str(workspace_dist), "--out-report", str(overlay_report)],
    )
    append_event(events_jsonl, run_id, "overlay_done", {"rc": oa_rc, "infra": oa_infra, "report": str(overlay_report)})
    if oa_infra or oa_rc != 0:
        code = 2 if (oa_infra or oa_rc == 2) else 1
        final = {
            "schema": "factory_final_report_v1",
            "ok": False,
            "exit_code": code,
            "run_id": run_id,
            "product_id": product_id,
            "run_dir": str(run_dir),
            "entrypoint": "",
            "release_id": "",
            "release_zip": "",
            "release_hashes": "",
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_json),
            "acceptance_ok": False,
            "run_acceptance": "YES",
            "acceptance_report": "",
            "acceptance_embedded": "",
            "error": {"kind": ("infra" if code == 2 else "fail"), "type": "overlay_failed", "message": "see overlay_report"},
        }
        write_json_no_bom(final_json, final)
        emit_final_and_exit(final, code)

    # overlay hash & customer release id
    overlay_rep = json.loads(overlay_report.read_text(encoding="utf-8-sig"))
    oh = overlay_hash_from_report(overlay_rep, base_rel)
    cust_rel = derive_customer_release_id(product_id, base_rel, request_id, oh)
    append_event(events_jsonl, run_id, "release_id_derived", {"customer_release_id": cust_rel, "overlay_hash": oh})

    # ---- step 5: pack custom release ----
    p_obj, p_infra, p_rc = run_py_module(
        repo, evidence_dir, "pack_custom",
        "args.foundry.release_pack_v0",
        ["--product-id", product_id, "--product-dist", str(workspace_dist), "--release-id", cust_rel],
    )
    append_event(events_jsonl, run_id, "pack_done", {"rc": p_rc, "infra": p_infra, "customer_release_id": cust_rel})
    if p_infra or p_rc != 0:
        code = 2 if (p_infra or p_rc == 2) else 1
        final = {
            "schema": "factory_final_report_v1",
            "ok": False,
            "exit_code": code,
            "run_id": run_id,
            "product_id": product_id,
            "run_dir": str(run_dir),
            "entrypoint": "",
            "release_id": cust_rel,
            "release_zip": "",
            "release_hashes": "",
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_json),
            "acceptance_ok": False,
            "run_acceptance": "YES",
            "acceptance_report": str(workspace_dist / "acceptance_gate.json"),
            "acceptance_embedded": str(workspace_dist / "acceptance_gate.json"),
            "customization": {
                "request_path": str(req_path),
                "request_normalized_json": str(req_norm),
                "base_release_id": base_rel,
                "unpack_report_json": str(unpack_report),
                "overlay_report_json": str(overlay_report),
                "workspace_dist": str(workspace_dist),
                "overlay_hash": oh,
                "customer_release_id": cust_rel,
            },
            "error": {"kind": ("infra" if code == 2 else "fail"), "type": "pack_failed", "message": cust_rel},
        }
        write_json_no_bom(final_json, final)
        emit_final_and_exit(final, code)

    release_zip = str(p_obj.get("release_zip") or "")
    release_hashes = str(p_obj.get("release_hashes") or "")

    # ---- step 6: verify new release ----
    vn_obj, vn_infra, vn_rc = run_py_module(
        repo, evidence_dir, "verify_new",
        "args.foundry.release_verify_v0",
        ["--release-id", cust_rel],
    )
    append_event(events_jsonl, run_id, "verify_new_done", {"rc": vn_rc, "infra": vn_infra, "customer_release_id": cust_rel})
    if vn_infra or vn_rc != 0:
        code = 2 if (vn_infra or vn_rc == 2) else 1
        final = {
            "schema": "factory_final_report_v1",
            "ok": False,
            "exit_code": code,
            "run_id": run_id,
            "product_id": product_id,
            "run_dir": str(run_dir),
            "entrypoint": "",
            "release_id": cust_rel,
            "release_zip": release_zip,
            "release_hashes": release_hashes,
            "evidence_dir": str(evidence_dir),
            "events_jsonl": str(events_jsonl),
            "final_report_json": str(final_json),
            "acceptance_ok": False,
            "run_acceptance": "YES",
            "acceptance_report": str(workspace_dist / "acceptance_gate.json"),
            "acceptance_embedded": str(workspace_dist / "acceptance_gate.json"),
            "customization": {
                "request_path": str(req_path),
                "request_normalized_json": str(req_norm),
                "base_release_id": base_rel,
                "unpack_report_json": str(unpack_report),
                "overlay_report_json": str(overlay_report),
                "workspace_dist": str(workspace_dist),
                "overlay_hash": oh,
                "customer_release_id": cust_rel,
            },
            "error": {"kind": ("infra" if code == 2 else "fail"), "type": "new_release_verify_failed", "message": cust_rel},
        }
        write_json_no_bom(final_json, final)
        emit_final_and_exit(final, code)

    # ---- OK final ----
    final = {
        "schema": "factory_final_report_v1",
        "ok": True,
        "exit_code": 0,
        "run_id": run_id,
        "product_id": product_id,
        "run_dir": str(run_dir),
        "entrypoint": "",
        "release_id": cust_rel,
        "release_zip": release_zip,
        "release_hashes": release_hashes,
        "evidence_dir": str(evidence_dir),
        "events_jsonl": str(events_jsonl),
        "final_report_json": str(final_json),
        "acceptance_ok": True,
        "run_acceptance": "YES",
        "acceptance_report": str(workspace_dist / "acceptance_gate.json"),
        "acceptance_embedded": str(workspace_dist / "acceptance_gate.json"),
        "customization": {
            "request_path": str(req_path),
            "request_normalized_json": str(req_norm),
            "base_release_id": base_rel,
            "unpack_report_json": str(unpack_report),
            "overlay_report_json": str(overlay_report),
            "workspace_dist": str(workspace_dist),
            "overlay_hash": oh,
            "customer_release_id": cust_rel,
        },
    }
    write_json_no_bom(final_json, final)
    append_event(events_jsonl, run_id, "done", {"ok": True, "customer_release_id": cust_rel})
    emit_final_and_exit(final, 0)


if __name__ == "__main__":
    main()