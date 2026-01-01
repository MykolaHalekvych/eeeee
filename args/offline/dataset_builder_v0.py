
@'
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# -----------------------------
# Paths
# -----------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"
DATASETS_DIR = REPO_ROOT / "args" / "offline" / "datasets"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> Dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    if not isinstance(obj, dict):
        raise ValueError(f"{path} is not a JSON object")
    return obj


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
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


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _list_latest_run_reports(limit: int = 50) -> List[Path]:
    reports = [p for p in LOGS_DIR.glob("run_report_*_paper.json") if p.is_file()]
    reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[: max(1, int(limit))]


def _run_id_from_report_name(p: Path) -> Optional[str]:
    name = p.name
    if not (name.startswith("run_report_") and name.endswith("_paper.json")):
        return None
    rid = name[len("run_report_") : -len("_paper.json")].strip()
    return rid or None


@dataclass(frozen=True)
class BuildSpec:
    run_ids: List[str]
    created_at_utc: str
    source: str
    include_fields: List[str]


def _default_include_fields() -> List[str]:
    return [
        "run_id",
        "index",
        "ts",
        "instrument",
        "timeframe",
        "mode",
        "ma_decision",
        "reason",
        "gate_reason",
        "intent_kind",
        "payload_kind",
        "orderType",
        "side",
        "lmtPrice",
        "qty",
    ]


def _extract_mode_from_report(report: Dict[str, Any]) -> Optional[str]:
    mode = None
    re = report.get("risk_envelope")
    if isinstance(re, dict):
        mode = re.get("mode")
    if mode is None:
        ma = report.get("ma_report")
        if isinstance(ma, dict):
            re2 = ma.get("risk_envelope")
            if isinstance(re2, dict):
                mode = re2.get("mode")
    return mode


def _extract_row(
    *,
    run_id: str,
    report: Dict[str, Any],
    events_row: Optional[Dict[str, Any]],
    orders_paper_row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    row: Dict[str, Any] = {"run_id": run_id}
    row["instrument"] = report.get("instrument")
    row["timeframe"] = report.get("timeframe")
    row["mode"] = _extract_mode_from_report(report)

    if isinstance(orders_paper_row, dict):
        row["index"] = orders_paper_row.get("index")
        row["ts"] = orders_paper_row.get("ts")
        row["ma_decision"] = orders_paper_row.get("ma_decision")
        row["reason"] = orders_paper_row.get("reason")

    if isinstance(events_row, dict):
        if row.get("index") is None:
            row["index"] = events_row.get("index")
        if row.get("ts") is None:
            row["ts"] = events_row.get("ts")
        if "gate_reason" in events_row:
            row["gate_reason"] = events_row.get("gate_reason")
        if row.get("mode") is None and "mode" in events_row:
            row["mode"] = events_row.get("mode")

    return row


def _coerce_payload_features(payload_row: Dict[str, Any], out_row: Dict[str, Any]) -> None:
    out_row["payload_kind"] = payload_row.get("payload_kind")
    if "gate_reason" in payload_row and out_row.get("gate_reason") is None:
        out_row["gate_reason"] = payload_row.get("gate_reason")

    # intent kind if present
    if out_row.get("intent_kind") is None:
        out_row["intent_kind"] = payload_row.get("intent_kind") or payload_row.get("intent_kind_raw")

    order = payload_row.get("order")
    if isinstance(order, dict):
        out_row["orderType"] = order.get("orderType")
        out_row["side"] = order.get("action")
        out_row["lmtPrice"] = order.get("lmtPrice")
        out_row["qty"] = order.get("totalQuantity")
    else:
        out_row["orderType"] = None
        out_row["side"] = None
        out_row["lmtPrice"] = None
        out_row["qty"] = None


def build_dataset(
    *,
    run_ids: Sequence[str],
    out_dir: Path,
    include_fields: Sequence[str],
) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)

    inputs: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []

    for rid in run_ids:
        report_path = LOGS_DIR / f"run_report_{rid}_paper.json"
        if not report_path.exists():
            continue
        report = _read_json(report_path)

        events_path = DATA_DIR / f"events_run_{rid}.jsonl"
        orders_paper_path = DATA_DIR / f"orders_paper_{rid}.jsonl"
        payload_path = DATA_DIR / f"orders_payload_{rid}.jsonl"

        events_by_idx: Dict[Any, Dict[str, Any]] = {}
        for e in _iter_jsonl(events_path):
            idx = e.get("index")
            if idx is not None and idx not in events_by_idx:
                events_by_idx[idx] = e

        paper_by_idx: Dict[Any, Dict[str, Any]] = {}
        for p in _iter_jsonl(orders_paper_path):
            idx = p.get("index")
            if idx is not None:
                paper_by_idx[idx] = p

        payload_by_idx: Dict[Any, Dict[str, Any]] = {}
        for pl in _iter_jsonl(payload_path):
            idx = pl.get("index")
            if idx is not None:
                payload_by_idx[idx] = pl

        def _idx_key(x: Any) -> int:
            try:
                return int(x)
            except Exception:
                return 0

        indices = sorted(set(paper_by_idx.keys()) | set(payload_by_idx.keys()) | set(events_by_idx.keys()), key=_idx_key)

        for idx in indices:
            base = _extract_row(
                run_id=rid,
                report=report,
                events_row=events_by_idx.get(idx),
                orders_paper_row=paper_by_idx.get(idx),
            )
            pl = payload_by_idx.get(idx)
            if isinstance(pl, dict):
                _coerce_payload_features(pl, base)

            final = {k: base.get(k) for k in include_fields}
            rows.append(final)

        inputs.append(
            {
                "run_id": rid,
                "report_path": str(report_path),
                "report_sha256": _sha256_file(report_path),
                "events_path": str(events_path) if events_path.exists() else None,
                "events_sha256": _sha256_file(events_path) if events_path.exists() else None,
                "orders_paper_path": str(orders_paper_path) if orders_paper_path.exists() else None,
                "orders_paper_sha256": _sha256_file(orders_paper_path) if orders_paper_path.exists() else None,
                "payload_path": str(payload_path) if payload_path.exists() else None,
                "payload_sha256": _sha256_file(payload_path) if payload_path.exists() else None,
            }
        )

    dataset_path = out_dir / "dataset.jsonl"
    with dataset_path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    spec = BuildSpec(
        run_ids=list(run_ids),
        created_at_utc=_utc_now_z(),
        source="run_report_*_paper + events_run + orders_paper + orders_payload",
        include_fields=list(include_fields),
    )
    spec_hash = _sha256_text(_stable_json(spec.__dict__))
    dataset_sha = _sha256_file(dataset_path)

    manifest = {
        "schema": "dataset_manifest_v0",
        "created_at_utc": spec.created_at_utc,
        "run_ids": list(run_ids),
        "rows": len(rows),
        "dataset_path": str(dataset_path),
        "dataset_sha256": dataset_sha,
        "spec": spec.__dict__,
        "spec_sha256": spec_hash,
        "inputs": inputs,
    }

    _write_json(out_dir / "dataset_manifest.json", manifest)
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser("dataset_builder_v0")
    ap.add_argument("--latest", type=int, default=20, help="Use latest N run_report_*_paper.json")
    ap.add_argument("--out", default="", help="Output dataset directory name (default auto)")
    ap.add_argument("--fields", default="", help="Comma-separated list of fields (default v0)")
    args = ap.parse_args(argv)

    n = max(1, int(args.latest))
    reports = _list_latest_run_reports(limit=n)
    run_ids: List[str] = []
    for p in reports:
        rid = _run_id_from_report_name(p)
        if rid:
            run_ids.append(rid)

    if not run_ids:
        print("NO_RUN_IDS")
        return 2

    include_fields = _default_include_fields()
    if args.fields.strip():
        include_fields = [x.strip() for x in args.fields.split(",") if x.strip()]

    id_src = _stable_json({"run_ids": run_ids, "fields": include_fields})
    dataset_id = hashlib.sha256(id_src.encode("utf-8")).hexdigest()[:12]

    out_name = args.out.strip() or f"ds_{dataset_id}"
    out_dir = DATASETS_DIR / out_name

    manifest = build_dataset(run_ids=run_ids, out_dir=out_dir, include_fields=include_fields)

    print("DATASET_BUILDER_V0")
    print(json.dumps({"ok": True, "out_dir": str(out_dir), "manifest_path": str(out_dir / "dataset_manifest.json"), "rows": manifest.get("rows"), "run_ids": run_ids}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'@ | Set-Content -Encoding UTF8 .\args\offline\dataset_builder_v0.py
