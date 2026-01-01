from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_DIR = REPO_ROOT / "args" / "offline"
MODELS_DIR = OFFLINE_DIR / "models"
REGISTRY_PATH = OFFLINE_DIR / "model_registry.json"

DATA_DIR = REPO_ROOT / "args" / "data"
AS_MODEL_VERSION_PATH = DATA_DIR / "as_model_version.json"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json_first_obj(path: Path) -> Dict[str, Any]:
    s = path.read_text(encoding="utf-8-sig", errors="replace").strip()
    if not s:
        raise ValueError(f"{path} empty")
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(s)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} first JSON is not object")
    return obj


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _latest_model_dir() -> Path:
    if not MODELS_DIR.exists():
        raise FileNotFoundError(f"models dir missing: {MODELS_DIR}")
    cands = [p for p in MODELS_DIR.iterdir() if p.is_dir() and (p / "model_manifest.json").exists()]
    if not cands:
        raise FileNotFoundError("no model_manifest.json under args/offline/models")
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def _load_registry() -> Dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {"schema": "model_registry_v0", "updated_at_utc": _utc_now_z(), "models": []}
    obj = _read_json_first_obj(REGISTRY_PATH)
    if not isinstance(obj.get("models"), list):
        obj["models"] = []
    obj.setdefault("schema", "model_registry_v0")
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


def _promote(model_id: str) -> Dict[str, Any]:
    obj = {"schema": "as_model_version_v0", "as_model_version": str(model_id), "set_at_utc": _utc_now_z()}
    _write_json(AS_MODEL_VERSION_PATH, obj)
    return obj


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser("model_registry_v0")
    ap.add_argument("--model", default="", help="Model dir name under args/offline/models (default: latest)")
    ap.add_argument("--promote", action="store_true", help="If eval_gate ok, set args/data/as_model_version.json")
    args = ap.parse_args(argv)

    model_dir = MODELS_DIR / args.model.strip() if args.model.strip() else _latest_model_dir()

    mm_path = model_dir / "model_manifest.json"
    eg_path = model_dir / "eval_gate_report.json"

    mm = _read_json_first_obj(mm_path)
    eg = _read_json_first_obj(eg_path) if eg_path.exists() else {"schema": "eval_gate_v0", "ok": False, "fails": {"missing_eval": "NO_EVAL_REPORT"}}

    model_id = str(mm.get("model_id") or model_dir.name.replace("model_", ""))

    reg = _load_registry()

    rec = {
        "model_id": model_id,
        "model_dir": str(model_dir),
        "created_at_utc": mm.get("created_at_utc"),
        "dataset_dir": mm.get("dataset_dir"),
        "dataset_sha256": mm.get("dataset_sha256"),
        "code_sha256": mm.get("code_sha256"),
        "eval_ok": bool(eg.get("ok") is True),
        "eval_fails": eg.get("fails") if isinstance(eg.get("fails"), dict) else {},
        "promoted": False,
        "promoted_at_utc": None,
    }

    # promote only if requested and eval_ok
    promoted_obj = None
    if args.promote and bool(eg.get("ok") is True):
        promoted_obj = _promote(model_id)
        rec["promoted"] = True
        rec["promoted_at_utc"] = promoted_obj.get("set_at_utc")

    reg["updated_at_utc"] = _utc_now_z()
    _upsert_model(reg, rec)
    _write_json(REGISTRY_PATH, reg)

    out = {
        "ok": True,
        "model_id": model_id,
        "model_dir": str(model_dir),
        "eval_ok": rec["eval_ok"],
        "registry_path": str(REGISTRY_PATH),
        "promoted": bool(rec["promoted"]),
        "as_model_version_path": str(AS_MODEL_VERSION_PATH),
        "as_model_version_written": promoted_obj if promoted_obj else None,
    }

    print("MODEL_REGISTRY_V0")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
