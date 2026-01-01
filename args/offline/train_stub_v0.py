from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_DIR = REPO_ROOT / "args" / "offline"
DATASETS_DIR = OFFLINE_DIR / "datasets"
MODELS_DIR = OFFLINE_DIR / "models"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    if not isinstance(obj, dict):
        raise ValueError(f"{path} is not a JSON object")
    return obj


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    yield obj
            except Exception:
                continue


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _model_id(dataset_sha: str, code_sha: str) -> str:
    x = hashlib.sha256(f"{dataset_sha}:{code_sha}".encode("utf-8")).hexdigest()
    return x[:12]


def _code_sha_self() -> str:
    return _sha256_file(Path(__file__).resolve())


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser("train_stub_v0")
    ap.add_argument("--dataset", default="", help="Dataset dir name under args/offline/datasets (default: latest)")
    args = ap.parse_args(argv)

    if args.dataset.strip():
        ds_dir = DATASETS_DIR / args.dataset.strip()
    else:
        cands = [p for p in DATASETS_DIR.iterdir() if p.is_dir() and (p / "dataset_manifest.json").exists()]
        if not cands:
            print("NO_DATASETS_FOUND")
            return 2
        cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        ds_dir = cands[0]

    manifest_path = ds_dir / "dataset_manifest.json"
    dataset_path = ds_dir / "dataset.jsonl"
    if not manifest_path.exists() or not dataset_path.exists():
        print(f"DATASET_INVALID: {ds_dir}")
        return 2

    man = _read_json(manifest_path)
    dataset_sha = str(man.get("dataset_sha256") or _sha256_file(dataset_path))
    code_sha = _code_sha_self()
    mid = _model_id(dataset_sha, code_sha)

    # Metrics
    cnt = 0
    missing_counts = Counter()
    payload_kind = Counter()
    ma_decision = Counter()
    gate_reason = Counter()
    intent_kind = Counter()

    for r in _iter_jsonl(dataset_path):
        cnt += 1
        for k, v in r.items():
            if v is None or v == "":
                missing_counts[k] += 1
        pk = str(r.get("payload_kind") or "")
        if pk:
            payload_kind[pk] += 1
        md = str(r.get("ma_decision") or "")
        if md:
            ma_decision[md] += 1
        gr = str(r.get("gate_reason") or "")
        if gr:
            gate_reason[gr] += 1
        ik = str(r.get("intent_kind") or "")
        if ik:
            intent_kind[ik] += 1

    metrics = {
        "rows": cnt,
        "payload_kind": dict(payload_kind),
        "ma_decision": dict(ma_decision),
        "gate_reason_top10": dict(gate_reason.most_common(10)),
        "intent_kind": dict(intent_kind),
        "missing_ratio_top10": {
            k: (missing_counts[k] / cnt if cnt else 0.0)
            for k, _ in missing_counts.most_common(10)
        },
    }

    model_dir = MODELS_DIR / f"model_{mid}"
    model_dir.mkdir(parents=True, exist_ok=True)

    model_manifest = {
        "schema": "model_manifest_v0",
        "created_at_utc": _utc_now_z(),
        "model_id": mid,
        "dataset_dir": str(ds_dir),
        "dataset_sha256": dataset_sha,
        "code_sha256": code_sha,
        "metrics": metrics,
        "release": {"promoted": False, "as_model_version": None},
    }

    (model_dir / "model_manifest.json").write_text(json.dumps(model_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("TRAIN_STUB_V0")
    print(json.dumps({"ok": True, "model_dir": str(model_dir), "model_id": mid, "manifest": str(model_dir / "model_manifest.json")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
