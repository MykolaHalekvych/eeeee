from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from args.offline.audit_log_v0 import AuditLog
from args.offline.evidence_history_v0 import atomic_write_json, sha256_file

REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_DIR = REPO_ROOT / "args" / "offline"
MODELS_DIR = OFFLINE_DIR / "models"
REGISTRY_PATH = OFFLINE_DIR / "model_registry.json"

DATA_DIR = REPO_ROOT / "args" / "data"
AS_MODEL_VERSION_PATH = DATA_DIR / "as_model_version.json"

AUDIT_PATH_DEFAULT = OFFLINE_DIR / "model_registry_events.jsonl"
AUDIT_KIND = "model_registry_audit_v1"


def _utc_now_z() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _read_json_first_obj(path: Path) -> Dict[str, Any]:
    s = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not s:
        raise ValueError(f"{path} empty")
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(s)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} first JSON is not object")
    return obj


def _latest_model_dir() -> Path:
    if not MODELS_DIR.exists():
        raise FileNotFoundError(f"models dir missing: {MODELS_DIR}")
    cands = [
        p
        for p in MODELS_DIR.iterdir()
        if p.is_dir() and (p / "model_manifest.json").exists()
    ]
    if not cands:
        raise FileNotFoundError("no model_manifest.json under args/offline/models")
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def _load_registry() -> Dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {
            "schema": "model_registry_v0",
            "updated_at_utc": _utc_now_z(),
            "models": [],
        }
    obj = _read_json_first_obj(REGISTRY_PATH)
    if not isinstance(obj.get("models"), list):
        obj["models"] = []
    obj.setdefault("schema", "model_registry_v0")
    obj.setdefault("updated_at_utc", _utc_now_z())
    return obj


def _upsert_model(reg: Dict[str, Any], rec: Dict[str, Any]) -> None:
    models = reg.get("models")
    if not isinstance(models, list):
        models = []
        reg["models"] = models
    mid = rec.get("model_id")
    for i, m in enumerate(models):
        if isinstance(m, dict) and m.get("model_id") == mid:
            models[i] = rec
            return
    models.append(rec)


def _promote_write(model_id: str) -> Dict[str, Any]:
    obj = {
        "schema": "as_model_version_v0",
        "as_model_version": str(model_id),
        "set_at_utc": _utc_now_z(),
    }
    atomic_write_json(AS_MODEL_VERSION_PATH, obj)
    return obj


def _bool_eval_ok(eg: Dict[str, Any]) -> bool:
    return bool(eg.get("ok") is True)


def _resolve_model_id(mm: Dict[str, Any], model_dir: Path) -> str:
    mid = mm.get("model_id")
    if isinstance(mid, str) and mid.strip():
        return mid.strip()
    # fallback: model_<id>
    name = model_dir.name
    if name.startswith("model_"):
        return name.replace("model_", "", 1)
    return name


def _build_record(
    model_id: str,
    model_dir: Path,
    mm: Dict[str, Any],
    eg: Dict[str, Any],
    promoted_obj: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "model_id": model_id,
        "model_dir": str(model_dir),
        "created_at_utc": mm.get("created_at_utc"),
        "dataset_dir": mm.get("dataset_dir"),
        "dataset_sha256": mm.get("dataset_sha256"),
        "code_sha256": mm.get("code_sha256"),
        "eval_ok": _bool_eval_ok(eg),
        "eval_fails": eg.get("fails") if isinstance(eg.get("fails"), dict) else {},
        "eval_warnings": eg.get("warnings")
        if isinstance(eg.get("warnings"), dict)
        else {},
        "eval_report_path": str((model_dir / "eval_gate_report.json")),
        "promoted": bool(promoted_obj is not None),
        "promoted_at_utc": (
            promoted_obj.get("set_at_utc") if isinstance(promoted_obj, dict) else None
        ),
    }
    return rec


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser("model_registry_v0")
    ap.add_argument(
        "--model",
        default="",
        help="Model dir name under args/offline/models (default: latest)",
    )
    ap.add_argument(
        "--promote",
        action="store_true",
        help="Promote model if eval_gate ok -> write args/data/as_model_version.json",
    )

    # Audit controls (Stage 8.5)
    ap.add_argument(
        "--audit-path",
        default=str(AUDIT_PATH_DEFAULT),
        help="Append-only audit JSONL path",
    )
    ap.add_argument(
        "--no-audit", action="store_true", help="Disable audit trail (NOT recommended)"
    )
    ap.add_argument(
        "--verify-audit",
        action="store_true",
        help="Verify audit log hash-chain and exit",
    )

    args = ap.parse_args(argv)

    generated_at_utc = _utc_now_z()
    audit_path = Path(args.audit_path)

    # Mode: verify audit only
    if args.verify_audit:
        log = AuditLog(audit_path, kind=AUDIT_KIND)
        ok, rep = log.verify()
        out = {
            "schema": "model_registry_v0",
            "generated_at_utc": generated_at_utc,
            "status": "PASS" if ok else "FAIL",
            "ok": bool(ok),
            "audit_path": str(audit_path),
            "audit_verify": rep,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if ok else 2

    # Normal mode
    ok: bool = True
    promoted_obj: Optional[Dict[str, Any]] = None
    model_dir: Optional[Path] = None
    model_id: Optional[str] = None
    mm: Dict[str, Any] = {}
    eg: Dict[str, Any] = {}

    audit = AuditLog(audit_path, kind=AUDIT_KIND)

    try:
        model_dir = (
            (MODELS_DIR / args.model.strip())
            if args.model.strip()
            else _latest_model_dir()
        )

        mm_path = model_dir / "model_manifest.json"
        eg_path = model_dir / "eval_gate_report.json"

        mm = _read_json_first_obj(mm_path)
        if eg_path.exists():
            eg = _read_json_first_obj(eg_path)
        else:
            eg = {
                "schema": "eval_gate_v0",
                "ok": False,
                "fails": {"missing_eval": "NO_EVAL_REPORT"},
            }

        model_id = _resolve_model_id(mm, model_dir)

        # Prepare promote gating
        eval_ok = _bool_eval_ok(eg)
        if args.promote and not eval_ok:
            ok = False  # promote requested but gate not ok

        # Audit: attempt (even if will fail promote gate; keeps forensic trail)
        if not args.no_audit:
            audit.append(
                "UPSERT_ATTEMPT",
                {
                    "model_id": model_id,
                    "model_dir": str(model_dir),
                    "eval_ok": bool(eval_ok),
                    "promote_requested": bool(args.promote),
                },
                actor="model_registry_v0",
            )

        # Promote (only if requested AND eval_ok)
        if args.promote and eval_ok:
            if not args.no_audit:
                audit.append(
                    "PROMOTE_ATTEMPT",
                    {"to_model_id": model_id},
                    actor="model_registry_v0",
                )

            promoted_obj = _promote_write(model_id)

            if not args.no_audit:
                audit.append(
                    "PROMOTE_APPLIED",
                    {
                        "to_model_id": model_id,
                        "as_model_version_path": "args/data/as_model_version.json",
                        "set_at_utc": promoted_obj.get("set_at_utc"),
                    },
                    actor="model_registry_v0",
                )

        # Update registry
        reg = _load_registry()
        rec = _build_record(model_id, model_dir, mm, eg, promoted_obj)
        reg["updated_at_utc"] = _utc_now_z()
        _upsert_model(reg, rec)

        # Write registry atomically
        atomic_write_json(REGISTRY_PATH, reg)
        registry_sha = sha256_file(REGISTRY_PATH)

        if not args.no_audit:
            audit.append(
                "UPSERT_APPLIED",
                {
                    "model_id": model_id,
                    "registry_path": "args/offline/model_registry.json",
                    "registry_sha256": registry_sha,
                    "promoted": bool(promoted_obj is not None),
                },
                actor="model_registry_v0",
            )

        out: Dict[str, Any] = {
            "schema": "model_registry_v0",
            "generated_at_utc": generated_at_utc,
            "status": "PASS" if ok else "FAIL",
            "ok": bool(ok),
            "model_id": model_id,
            "model_dir": str(model_dir),
            "eval_ok": bool(_bool_eval_ok(eg)),
            "registry_path": str(REGISTRY_PATH),
            "registry_sha256": registry_sha,
            "promoted": bool(promoted_obj is not None),
            "as_model_version_path": str(AS_MODEL_VERSION_PATH),
            "as_model_version_written": promoted_obj if promoted_obj else None,
            "audit_path": str(audit_path),
        }

        if args.promote and not _bool_eval_ok(eg):
            out["error"] = {
                "code": "PROMOTE_BLOCKED_EVAL_NOT_OK",
                "message": "promote requested but eval_gate ok!=true",
            }

        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if ok else 2

    except Exception as e:
        # Hard FAIL. If audit breaks here, we still return JSON.
        out = {
            "schema": "model_registry_v0",
            "generated_at_utc": generated_at_utc,
            "status": "FAIL",
            "ok": False,
            "error": {"type": type(e).__name__, "message": str(e)},
            "audit_path": str(audit_path),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
