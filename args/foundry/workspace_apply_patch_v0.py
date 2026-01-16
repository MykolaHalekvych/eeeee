from __future__ import annotations

import argparse
import json
import os
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "workspace_apply_patch_v0"

RC_OK = 0
RC_FAIL = 1
RC_INFRA = 2

STEP = "apply_patch"


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_rel(p: Any) -> Path:
    if not isinstance(p, str) or not p:
        raise ValueError("path must be non-empty string")

    # Normalize Windows separators to canonical forward slashes.
    p = p.replace("\\", "/")

    if p.startswith("/") or p.startswith("~") or ":" in p:
        raise ValueError("absolute paths not allowed")
    parts = p.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError("path traversal not allowed")
    return Path(*parts)


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json_utf8sig(path: Path) -> dict:
    try:
        txt = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise
    except Exception as e:
        raise OSError(f"failed to read json: {path}") from e
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid json: {path}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"json root must be object: {path}")
    return obj


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: dict, pretty: bool = True) -> None:
    if pretty:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    else:
        text = json.dumps(obj, ensure_ascii=False)
    atomic_write_text(path, text)


def append_event(events_path: Path, run_id: str, kind: str, data: dict | None = None) -> None:
    ev = {
        "schema": "event_v0",
        "ts_utc": utc_ts(),
        "run_id": run_id,
        "step": SCHEMA,
        "kind": kind,
        "data": data or {},
    }
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def resolve_template_source(repo: Path, kit_id: str, product_id: str) -> tuple[dict, Path | None]:
    # Priority:
    # 1) manifests/template_packs/<kit_id>
    # 2) templates/<product_id>
    cand1 = repo / "manifests" / "template_packs" / kit_id
    if kit_id and cand1.is_dir():
        return ({"kind": "manifest_template_pack", "path": str(cand1)}, cand1)

    cand2 = repo / "templates" / product_id
    if product_id and cand2.is_dir():
        return ({"kind": "product_templates_fallback", "path": str(cand2)}, cand2)

    return ({"kind": "missing", "path": ""}, None)


def classify_exception(e: Exception) -> tuple[int, str]:
    # FAIL(1): input/config/data errors
    if isinstance(e, (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError)):
        return (RC_FAIL, "fail")
    # INFRA(2): OS/runtime errors
    if isinstance(e, (PermissionError, OSError, shutil.Error)):
        return (RC_INFRA, "infra")
    return (RC_INFRA, "infra")


def main() -> int:
    ap = argparse.ArgumentParser(description="Create workspace from template and apply codegen patch JSON (standard runner)")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--product-id", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--codegen", required=True, help="runs/<run_id>/codegen_output.json")
    ap.add_argument("--workspaces-dir", default=None)
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    run_id = str(args.run_id)
    product_id = str(args.product_id)

    # Canonical run dir + evidence
    run_dir = (repo / "args" / "data" / "runs" / run_id).resolve()
    evidence_dir = run_dir / "evidence"
    events_path = run_dir / "events.jsonl"
    final_report_path = run_dir / "final_report.json"
    stderr_path = evidence_dir / f"{STEP}.stderr.txt"
    stdout_path = evidence_dir / f"{STEP}.stdout.json"
    template_source_path = evidence_dir / "template_source.json"

    # State carried into error reports when possible
    kit_id = ""
    allowed_paths: list[str] | None = None
    allowed_paths_norm: set[str] | None = None
    template_source: dict = {"kind": "unknown", "path": ""}
    template_root: Path | None = None
    ws_root: Path | None = None
    codegen_path: Path | None = None

    append_event(events_path, run_id, "start", {"repo": str(repo), "product_id": product_id})

    try:
        # Require product manifest to exist (product identity)
        manifest_path = repo / "manifests" / "products" / f"{product_id}.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"product manifest not found: {manifest_path}")
        _ = read_json_utf8sig(manifest_path)  # not used for template selection anymore, but validates existence

        # Require job_request.json (security + kit_id)
        jr_path = run_dir / "job_request.json"
        if not jr_path.exists():
            raise FileNotFoundError(f"job_request.json not found: {jr_path}")
        jr = read_json_utf8sig(jr_path)

        kit_id = str(jr.get("kit_id") or "")
        jr_product = str(jr.get("product_id") or "")
        if jr_product and jr_product != product_id:
            raise ValueError(f"job_request.product_id mismatch: {jr_product} != {product_id}")

        apaths = jr.get("allowed_paths")
        if apaths is not None:
            if not isinstance(apaths, list) or not all(isinstance(x, str) for x in apaths):
                raise ValueError("job_request.allowed_paths must be an array of strings")
            allowed_paths = apaths

            allowed_paths_norm = set()
            for x in apaths:
                y = x.replace('\\', '/')
                if y.startswith('./'):
                    y = y[2:]
                allowed_paths_norm.add(y)
        append_event(events_path, run_id, "loaded_job_request", {"kit_id": kit_id, "allowed_paths": allowed_paths is not None})

        # Resolve template source (P4)
        template_source, template_root = resolve_template_source(repo, kit_id, product_id)

        # Evidence: always record template_source selection
        atomic_write_json(
            template_source_path,
            {"schema": "template_source_v1", "ts_utc": utc_ts(), "run_id": run_id, "kit_id": kit_id, "product_id": product_id, "template_source": template_source},
            pretty=True,
        )

        if template_root is None:
            raise ValueError(f"template_source_missing: kit_id={kit_id} product_id={product_id}")

        append_event(events_path, run_id, "template_source_resolved", {"template_source": template_source})

        # Workspace root
        workspaces_dir = Path(args.workspaces_dir).resolve() if args.workspaces_dir else (repo / "args" / "data" / "workspaces")
        ws_root = (workspaces_dir / run_id / "workspace").resolve()

        # Reset workspace
        if ws_root.exists():
            shutil.rmtree(ws_root)
        ws_root.parent.mkdir(parents=True, exist_ok=True)

        # Copy template -> workspace
        shutil.copytree(template_root, ws_root)
        append_event(events_path, run_id, "workspace_created", {"workspace": str(ws_root)})

        # Resolve codegen path
        codegen_path = Path(args.codegen)
        if not codegen_path.is_absolute():
            codegen_path = (repo / codegen_path).resolve()
        if not codegen_path.exists():
            raise FileNotFoundError(f"codegen_output.json not found: {codegen_path}")

        patch = read_json_utf8sig(codegen_path)
        if patch.get("schema") != "codegen_patch_v0":
            raise ValueError("codegen_output.json must be schema=codegen_patch_v0")

        ops = patch.get("ops", [])
        if not isinstance(ops, list) or not ops:
            raise ValueError("patch.ops must be non-empty array")

        written: list[str] = []
        blocked: list[str] = []

        ws_root_resolved = ws_root.resolve()

        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                raise ValueError(f"ops[{i}] must be object")
            if op.get("op") != "write_file":
                raise ValueError(f"ops[{i}].op must be write_file")

            rel = safe_rel(op.get("path"))
            rel_s = rel.as_posix()

            if allowed_paths_norm is not None and rel_s not in allowed_paths_norm:
                blocked.append(rel_s)
                continue

            full = (ws_root_resolved / rel).resolve()
            # Must stay under workspace root
            try:
                full.relative_to(ws_root_resolved)
            except Exception as e:
                raise ValueError(f"resolved path escapes workspace root: {rel_s}") from e

            content = op.get("content", "")
            if not isinstance(content, str):
                raise ValueError(f"ops[{i}].content must be string")

            full.parent.mkdir(parents=True, exist_ok=True)
            with full.open("w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            written.append(rel_s)

        if blocked:
            raise ValueError(f"blocked writes outside allowed_paths: {blocked}")

        report = {
            "schema": SCHEMA,
            "ok": True,
            "exit_code": RC_OK,
            "ts_utc": utc_ts(),
            "repo": str(repo),
            "run_id": run_id,
            "product_id": product_id,
            "kit_id": kit_id,
            "template_source": template_source,
            "workspace": str(ws_root),
            "codegen_path": str(codegen_path),
            "codegen_sha256": sha256_file(codegen_path),
            "files_written": written,
            "allowed_paths": allowed_paths,
        }

        # Always write final_report + evidence stdout
        atomic_write_json(final_report_path, report, pretty=True)
        atomic_write_json(stdout_path, report, pretty=False)

        append_event(events_path, run_id, "ok", {"files_written": len(written)})

        print(json.dumps(report, ensure_ascii=False))
        return RC_OK

    except Exception as e:
        rc, kind = classify_exception(e)

        # Always write stderr evidence (best effort)
        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            tb = traceback.format_exc()
            atomic_write_text(stderr_path, tb)
        except Exception:
            # Evidence write failed; do not print anything besides JSON stdout
            pass

        err = {
            "kind": kind,
            "type": e.__class__.__name__,
            "message": str(e),
        }

        report = {
            "schema": SCHEMA,
            "ok": False,
            "exit_code": rc,
            "ts_utc": utc_ts(),
            "repo": str(repo),
            "run_id": run_id,
            "product_id": product_id,
            "kit_id": kit_id,
            "template_source": template_source,
            "workspace": str(ws_root) if ws_root else "",
            "codegen_path": str(codegen_path) if codegen_path else "",
            "error": err,
        }

        # Always write final_report + evidence stdout (best effort)
        try:
            atomic_write_json(final_report_path, report, pretty=True)
            atomic_write_json(stdout_path, report, pretty=False)
        except Exception:
            pass

        try:
            append_event(events_path, run_id, "error", {"error": err})
        except Exception:
            pass

        print(json.dumps(report, ensure_ascii=False))
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
