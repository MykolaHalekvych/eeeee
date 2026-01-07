from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

class ManifestError(RuntimeError):
    pass

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        return json.loads(raw)
    except FileNotFoundError:
        raise ManifestError(f"manifest not found: {path}")
    except Exception as e:
        raise ManifestError(f"manifest parse error: {path} :: {type(e).__name__}: {e}")

@dataclass(frozen=True)
class Factory:
    factory_id: str
    kind: str
    description: str
    default_out_dir: str

@dataclass(frozen=True)
class Product:
    product_id: str
    title: str
    version: str
    include_paths: List[str]
    runbook_template_path: str

def load_factories(repo_root: Path) -> Dict[str, Factory]:
    p = repo_root / "manifests" / "factories.json"
    obj = _read_json(p)

    if obj.get("schema") != "engine_factories_v0":
        raise ManifestError("factories.json schema must be engine_factories_v0")

    arr = obj.get("factories")
    if not isinstance(arr, list) or len(arr) == 0:
        raise ManifestError("factories.json must contain non-empty factories[]")

    out: Dict[str, Factory] = {}
    for it in arr:
        if not isinstance(it, dict):
            continue
        fid = str(it.get("factory_id", "")).strip()
        if not fid:
            raise ManifestError("factory missing factory_id")
        out[fid] = Factory(
            factory_id=fid,
            kind=str(it.get("kind", "local")),
            description=str(it.get("description", "")),
            default_out_dir=str(it.get("default_out_dir", "dist")),
        )
    return out

def load_product(repo_root: Path, product_id: str) -> Product:
    p = repo_root / "manifests" / "products" / f"{product_id}.json"
    obj = _read_json(p)

    if obj.get("schema") != "engine_product_v0":
        raise ManifestError(f"{product_id}.json schema must be engine_product_v0")

    pid = str(obj.get("product_id", "")).strip()
    if pid != product_id:
        raise ManifestError(f"product_id mismatch: file={product_id} manifest={pid}")

    payload = obj.get("payload", {})
    inc = payload.get("include_paths", [])
    if not isinstance(inc, list) or len(inc) == 0:
        raise ManifestError("payload.include_paths must be non-empty list")

    runbook = obj.get("runbook", {})
    rb_path = str(runbook.get("template_path", "")).strip()
    if not rb_path:
        raise ManifestError("runbook.template_path is required")

    return Product(
        product_id=pid,
        title=str(obj.get("title", "")),
        version=str(obj.get("version", "")),
        include_paths=[str(x) for x in inc],
        runbook_template_path=rb_path,
    )
