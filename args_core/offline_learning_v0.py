from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .common_v0 import atomic_write_json, sha256_text, utc_now_iso


# -----------------------
# Utils
# -----------------------


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_last_jsonl_record(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    last = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    last = obj
            except Exception:
                continue
    return last


def _file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _ts_tag() -> str:
    # Unique enough per run: seconds + millis
    ms = int(time.time() * 1000) % 1000
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"_{ms:03d}"


def _iter_run_dirs(runs_root: Path) -> List[Path]:
    if not runs_root.exists():
        return []
    out = [p for p in runs_root.iterdir() if p.is_dir()]
    out.sort(key=lambda p: p.name)
    return out


def _ticket_stats(state: Dict[str, Any]) -> Dict[str, int]:
    """
    Extract ticket-level aggregate signals from Engine state.json.
    """
    tickets = state.get("tickets") or {}
    if not isinstance(tickets, dict):
        return {
            "tickets_total": 0,
            "tickets_terminal": 0,
            "tickets_done": 0,
            "tickets_failed": 0,
            "tickets_cancelled": 0,
            "tickets_filled": 0,
            "tickets_acked": 0,
            "tickets_partial": 0,
        }

    total = len(tickets)
    terminal = done = failed = cancelled = filled = acked = partial = 0

    for _, t in tickets.items():
        if not isinstance(t, dict):
            continue
        st = str(t.get("state") or "").upper()
        term = str(t.get("terminal") or "").upper()

        if st == "TERMINAL":
            terminal += 1
        if term == "DONE":
            done += 1
        elif term == "FAILED":
            failed += 1
        elif term == "CANCELLED":
            cancelled += 1
        elif term == "FILLED":
            filled += 1

        if st == "ACKED":
            acked += 1
        if st == "PARTIAL":
            partial += 1

    return {
        "tickets_total": total,
        "tickets_terminal": terminal,
        "tickets_done": done,
        "tickets_failed": failed,
        "tickets_cancelled": cancelled,
        "tickets_filled": filled,
        "tickets_acked": acked,
        "tickets_partial": partial,
    }


# -----------------------
# Dataset builder
# -----------------------


def build_dataset(
    *,
    runs_root: Path,
    out_csv: Path,
    evidence_root: Path,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    run_ids: List[str] = []

    for rd in _iter_run_dirs(runs_root):
        run_id = rd.name
        run_ids.append(run_id)

        state = _read_json(rd / "state.json")
        health = _read_json(rd / "health.json")
        rec_last = _read_last_jsonl_record(rd / "reconcile.jsonl")

        if not isinstance(state, dict):
            continue

        counters = state.get("counters") or {}
        if not isinstance(counters, dict):
            counters = {}

        # reconcile
        reconcile_ratio = None
        if isinstance(rec_last, dict):
            reconcile_ratio = rec_last.get("ratio")

        rr = float(reconcile_ratio) if reconcile_ratio is not None else 0.0
        last_error = state.get("last_error")
        err_flag = 1 if last_error else 0

        # core counters
        forbidden = int(counters.get("forbidden", 0) or 0)
        events_seen = int(counters.get("events_seen", 0) or 0)
        dedup_skips = int(counters.get("dedup_skips", 0) or 0)
        place_calls = int(counters.get("place_calls", 0) or 0)
        cancel_calls = int(counters.get("cancel_calls", 0) or 0)
        replace_calls = int(counters.get("replace_calls", 0) or 0)

        # health flags if available
        stop_flag = 0
        safe_mode = 0
        broker_connected = 0
        if isinstance(health, dict):
            stop_flag = 1 if health.get("stop_flag") else 0
            safe_mode = 1 if health.get("safe_mode") else 0
            broker_connected = 1 if health.get("broker_connected") else 0

        # ticket aggregates
        ts = _ticket_stats(state)

        # Label strategy (MVP but meaningful):
        # "good run" if reconcile>=0.99, forbidden=0, no last_error, not stop/safe.
        # (stop/safe are not "bad", but we exclude them from "clean" label)
        label = (
            1
            if (
                rr >= 0.99
                and forbidden == 0
                and err_flag == 0
                and stop_flag == 0
                and safe_mode == 0
            )
            else 0
        )

        row = {
            "run_id": run_id,
            "reconcile": rr,
            "events_seen": events_seen,
            "forbidden": forbidden,
            "dedup_skips": dedup_skips,
            "error_flag": err_flag,
            "stop_flag": stop_flag,
            "safe_mode": safe_mode,
            "broker_connected": broker_connected,
            "place_calls": place_calls,
            "cancel_calls": cancel_calls,
            "replace_calls": replace_calls,
            **ts,
            "label": label,
        }
        rows.append(row)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        list(rows[0].keys())
        if rows
        else [
            "run_id",
            "reconcile",
            "events_seen",
            "forbidden",
            "dedup_skips",
            "error_flag",
            "stop_flag",
            "safe_mode",
            "broker_connected",
            "place_calls",
            "cancel_calls",
            "replace_calls",
            "tickets_total",
            "tickets_terminal",
            "tickets_done",
            "tickets_failed",
            "tickets_cancelled",
            "tickets_filled",
            "tickets_acked",
            "tickets_partial",
            "label",
        ]
    )
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    meta = {
        "schema": "dataset_meta_v1",
        "ts_utc": utc_now_iso(),
        "rows": len(rows),
        "path": str(out_csv),
        "dataset_sha256": _file_sha256(out_csv),
        "features": [k for k in fieldnames if k not in {"run_id", "label"}],
        "label": "label",
        "runs_root": str(runs_root),
    }
    atomic_write_json(out_csv.with_suffix(out_csv.suffix + ".meta.json"), meta)

    # Evidence report
    ev_dir = evidence_root / "dataset_builder"
    _ensure_dir(ev_dir)
    report_path = ev_dir / f"{_ts_tag()}_dataset_builder_report.json"
    report = {
        "schema": "dataset_builder_report_v1",
        "ts_utc": utc_now_iso(),
        "runs_scanned": len(run_ids),
        "runs_used": len(rows),
        "runs_root": str(runs_root),
        "out_csv": str(out_csv),
        "out_csv_sha256": meta["dataset_sha256"],
        "meta_path": str(out_csv.with_suffix(out_csv.suffix + ".meta.json")),
        "features": meta["features"],
        "label": "label",
        "label_definition": "label=1 iff reconcile>=0.99 AND forbidden==0 AND last_error is null AND stop_flag==0 AND safe_mode==0",
        "sample_run_ids": run_ids[-20:],
    }
    atomic_write_json(report_path, report)

    meta["evidence_report"] = str(report_path)
    return meta


# -----------------------
# Simple deterministic baseline model
# -----------------------


@dataclass
class LinearModel:
    features: List[str]
    w: List[float]
    b: float

    def predict(self, x: Dict[str, float]) -> int:
        s = self.b
        for name, wi in zip(self.features, self.w):
            s += wi * float(x.get(name, 0.0))
        return 1 if s >= 0 else 0


def train_model(
    *,
    repo: Path,
    dataset_csv: Path,
    models_dir: Path,
    eval_threshold_acc: float,
    evidence_root: Path,
) -> Tuple[int, Dict[str, Any]]:
    meta = _read_json(dataset_csv.with_suffix(dataset_csv.suffix + ".meta.json")) or {}
    features = meta.get("features") if isinstance(meta, dict) else None
    if not isinstance(features, list) or not features:
        # fallback
        features = [
            "reconcile",
            "events_seen",
            "forbidden",
            "dedup_skips",
            "error_flag",
        ]

    data: List[Dict[str, Any]] = []
    with open(dataset_csv, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if not row:
                continue
            try:
                d: Dict[str, Any] = {k: row.get(k) for k in row.keys()}
                d["label"] = int(float(row.get("label", "0") or 0))
                for feat in features:
                    d[feat] = float(row.get(feat, "0") or 0)
                data.append(d)
            except Exception:
                continue

    pos = [d for d in data if d["label"] == 1]
    neg = [d for d in data if d["label"] == 0]
    if len(pos) == 0 or len(neg) == 0:
        report = {
            "ok": False,
            "reason": "need_both_classes",
            "pos": len(pos),
            "neg": len(neg),
        }
        # Evidence
        ev_dir = evidence_root / "eval_gate"
        _ensure_dir(ev_dir)
        report_path = ev_dir / f"{_ts_tag()}_eval_gate_report.json"
        atomic_write_json(
            report_path,
            {
                "schema": "eval_gate_report_v1",
                "ts_utc": utc_now_iso(),
                "decision": "FAIL",
                "exit_code": 2,
                "reason": "need_both_classes",
                "pos": len(pos),
                "neg": len(neg),
                "dataset": str(dataset_csv),
                "dataset_sha256": _file_sha256(dataset_csv),
                "features": features,
                "threshold_acc": float(eval_threshold_acc),
            },
        )
        return 2, {"ok": False, **report, "evidence_report": str(report_path)}

    def mean(ds: List[Dict[str, Any]]) -> List[float]:
        out = []
        for feat in features:
            out.append(sum(float(d[feat]) for d in ds) / max(1, len(ds)))
        return out

    mu1 = mean(pos)
    mu0 = mean(neg)
    w = [a - b for a, b in zip(mu1, mu0)]
    b = -0.5 * sum(wi * (a + b0) for wi, a, b0 in zip(w, mu1, mu0))

    model = LinearModel(features=features, w=w, b=b)

    # Evaluate
    tp = tn = fp = fn = 0
    correct = 0
    for d in data:
        x = {feat: float(d[feat]) for feat in features}
        y = int(d["label"])
        yhat = model.predict(x)
        correct += 1 if yhat == y else 0
        if y == 1 and yhat == 1:
            tp += 1
        elif y == 0 and yhat == 0:
            tn += 1
        elif y == 0 and yhat == 1:
            fp += 1
        elif y == 1 and yhat == 0:
            fn += 1

    acc = correct / max(1, len(data))

    # Deterministic model_id from (code_sha, data_sha, config)
    code_sha = _file_sha256(Path(__file__))
    data_sha = _file_sha256(dataset_csv)
    config = {"eval_threshold_acc": float(eval_threshold_acc), "features": features}
    model_id = sha256_text(
        json.dumps(
            {"code": code_sha, "data": data_sha, "config": config}, sort_keys=True
        )
    )[:12]

    out_dir = models_dir / model_id
    out_dir.mkdir(parents=True, exist_ok=True)

    model_json = {
        "schema": "linear_model_v1",
        "model_id": model_id,
        "features": features,
        "w": w,
        "b": b,
        "created_at_utc": utc_now_iso(),
    }
    atomic_write_json(out_dir / "model.json", model_json)

    manifest = {
        "schema": "model_manifest_v1",
        "model_id": model_id,
        "created_at_utc": utc_now_iso(),
        "code_sha256": code_sha,
        "dataset_sha256": data_sha,
        "config": config,
        "metrics": {
            "accuracy": acc,
            "rows": len(data),
            "pos": len(pos),
            "neg": len(neg),
            "tp": tp,
            "tn": tn,
            "fp": fp,
            "fn": fn,
        },
        "paths": {"model": str(out_dir / "model.json"), "dataset": str(dataset_csv)},
    }
    atomic_write_json(out_dir / "manifest.json", manifest)

    # Registry update (idempotent)
    reg_path = models_dir / "registry.json"
    reg = _read_json(reg_path)
    if not isinstance(reg, dict):
        reg = {"schema": "model_registry_v1", "models": {}}
    models = reg.get("models")
    if not isinstance(models, dict):
        models = {}
    models[model_id] = manifest
    reg["models"] = models
    reg["updated_at_utc"] = utc_now_iso()
    atomic_write_json(reg_path, reg)

    exit_code = 0 if acc >= float(eval_threshold_acc) else 2

    # Evidence report (eval gate)
    ev_dir = evidence_root / "eval_gate"
    _ensure_dir(ev_dir)
    report_path = ev_dir / f"{_ts_tag()}_eval_gate_report.json"
    decision = "PASS" if exit_code == 0 else "FAIL"
    atomic_write_json(
        report_path,
        {
            "schema": "eval_gate_report_v1",
            "ts_utc": utc_now_iso(),
            "decision": decision,
            "exit_code": exit_code,
            "threshold_acc": float(eval_threshold_acc),
            "observed_acc": acc,
            "confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
            "dataset": str(dataset_csv),
            "dataset_sha256": data_sha,
            "code_sha256": code_sha,
            "model_id": model_id,
            "manifest_path": str(out_dir / "manifest.json"),
            "why": "PASS iff accuracy >= threshold_acc",
        },
    )

    report = {
        "ok": True,
        "model_id": model_id,
        "accuracy": acc,
        "exit_code": exit_code,
        "out_dir": str(out_dir),
        "evidence_report": str(report_path),
    }
    return exit_code, report


def promote_model(
    *,
    control_plane_path: Path,
    models_dir: Path,
    model_id: str,
    evidence_root: Path,
) -> Dict[str, Any]:
    cp = _read_json(control_plane_path)
    if not isinstance(cp, dict):
        cp = {}

    reg = _read_json(models_dir / "registry.json")
    models = reg.get("models") if isinstance(reg, dict) else None
    if not isinstance(models, dict) or model_id not in models:
        # evidence promote FAIL
        ev_dir = evidence_root / "promote"
        _ensure_dir(ev_dir)
        report_path = ev_dir / f"{_ts_tag()}_promote_report.json"
        atomic_write_json(
            report_path,
            {
                "schema": "promote_report_v1",
                "ts_utc": utc_now_iso(),
                "ok": False,
                "reason": "model_id_not_in_registry",
                "model_id": model_id,
                "control_plane": str(control_plane_path),
                "registry": str(models_dir / "registry.json"),
            },
        )
        return {
            "ok": False,
            "reason": "model_id_not_in_registry",
            "model_id": model_id,
            "evidence_report": str(report_path),
        }

    old = cp.get("as_model_version")
    cp["as_model_version"] = model_id
    atomic_write_json(control_plane_path, cp)

    # evidence promote PASS
    ev_dir = evidence_root / "promote"
    _ensure_dir(ev_dir)
    report_path = ev_dir / f"{_ts_tag()}_promote_report.json"
    atomic_write_json(
        report_path,
        {
            "schema": "promote_report_v1",
            "ts_utc": utc_now_iso(),
            "ok": True,
            "old_as_model_version": old,
            "new_as_model_version": model_id,
            "control_plane": str(control_plane_path),
            "model_manifest": models[model_id].get("paths", {}).get("model")
            if isinstance(models[model_id], dict)
            else None,
            "registry": str(models_dir / "registry.json"),
        },
    )

    return {
        "ok": True,
        "control_plane": str(control_plane_path),
        "as_model_version": model_id,
        "evidence_report": str(report_path),
    }


# -----------------------
# CLI
# -----------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_b = sub.add_parser("build")
    ap_b.add_argument("--runs", required=True)
    ap_b.add_argument("--out", required=True)
    ap_b.add_argument("--evidence-root", default=r"args\offline\evidence")

    ap_t = sub.add_parser("train")
    ap_t.add_argument("--repo", required=True)
    ap_t.add_argument("--dataset", required=True)
    ap_t.add_argument("--models", required=True)
    ap_t.add_argument("--eval-threshold-acc", type=float, default=0.55)
    ap_t.add_argument("--evidence-root", default=r"args\offline\evidence")

    ap_p = sub.add_parser("promote")
    ap_p.add_argument("--control-plane", required=True)
    ap_p.add_argument("--models", required=True)
    ap_p.add_argument("--model-id", required=True)
    ap_p.add_argument("--evidence-root", default=r"args\offline\evidence")

    args = ap.parse_args()

    if args.cmd == "build":
        meta = build_dataset(
            runs_root=Path(args.runs),
            out_csv=Path(args.out),
            evidence_root=Path(args.evidence_root),
        )
        print(json.dumps(meta, ensure_ascii=False))
        return 0

    if args.cmd == "train":
        code, report = train_model(
            repo=Path(args.repo),
            dataset_csv=Path(args.dataset),
            models_dir=Path(args.models),
            eval_threshold_acc=float(args.eval_threshold_acc),
            evidence_root=Path(args.evidence_root),
        )
        print(json.dumps(report, ensure_ascii=False))
        return int(code)

    if args.cmd == "promote":
        rep = promote_model(
            control_plane_path=Path(args.control_plane),
            models_dir=Path(args.models),
            model_id=str(args.model_id),
            evidence_root=Path(args.evidence_root),
        )
        print(json.dumps(rep, ensure_ascii=False))
        return 0 if rep.get("ok") else 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
