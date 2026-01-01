from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = "soak_report_v0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_z(s: str) -> Optional[datetime]:
    try:
        s2 = (s or "").strip()
        if not s2:
            return None
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
    return out


def _consecutive_max(flags: List[bool]) -> int:
    m = 0
    cur = 0
    for x in flags:
        if x:
            cur += 1
            m = max(m, cur)
        else:
            cur = 0
    return m


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute PASS/WARN/FAIL over soak_history window.")
    ap.add_argument("--history", default="", help="History JSONL (default args/logs/soak_history.jsonl)")
    ap.add_argument("--window-hours", type=int, default=24)
    ap.add_argument("--expected-interval-s", type=int, default=900)

    ap.add_argument("--fail-rate-warn", type=float, default=0.01)
    ap.add_argument("--fail-rate-fail", type=float, default=0.05)

    ap.add_argument("--max-gap-s-warn", type=int, default=3600)
    ap.add_argument("--max-gap-s-fail", type=int, default=7200)

    ap.add_argument("--consec-fail-warn", type=int, default=2)
    ap.add_argument("--consec-fail-fail", type=int, default=4)

    ap.add_argument("--min-sample-frac-warn", type=float, default=0.8)
    ap.add_argument("--min-sample-frac-fail", type=float, default=0.6)

    ap.add_argument("--out", default="", help="Write report JSON (default args/data/soak_report.json)")
    ap.add_argument("--archive", action="store_true", help="Also write timestamped copy under args/logs/soak_reports/")
    args = ap.parse_args()

    repo = _repo_root()
    history = Path(args.history) if args.history else (repo / "args" / "logs" / "soak_history.jsonl")
    out_path = Path(args.out) if args.out else (repo / "args" / "data" / "soak_report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    window = timedelta(hours=max(1, args.window_hours))
    t_min = now - window

    rows = _read_jsonl(history)

    # Extract (ts, level, reason, failures[])
    items: List[Tuple[datetime, str, str, List[str]]] = []
    for r in rows:
        soak = r.get("soak") if isinstance(r.get("soak"), dict) else None
        if not soak:
            continue
        ts = _parse_iso_z(soak.get("ts_utc") or r.get("ts_utc") or "")
        if not ts or ts < t_min or ts > now + timedelta(minutes=5):
            continue
        level = str(soak.get("level") or "").upper()
        reason = str(soak.get("reason") or "")
        failures = []
        details = soak.get("details")
        if isinstance(details, dict):
            failures = details.get("failures") or []
        if not isinstance(failures, list):
            failures = []
        failures = [str(x) for x in failures]
        items.append((ts, level, reason, failures))

    items.sort(key=lambda x: x[0])

    sample_count = len(items)
    expected = int(window.total_seconds() // max(1, args.expected_interval_s))
    expected = max(1, expected)

    pass_count = sum(1 for _, lvl, _, _ in items if lvl == "PASS")
    warn_count = sum(1 for _, lvl, _, _ in items if lvl == "WARN")
    fail_count = sum(1 for _, lvl, _, _ in items if lvl == "FAIL")

    # gaps
    max_gap_s = 0.0
    if sample_count >= 2:
        for i in range(1, sample_count):
            gap = (items[i][0] - items[i - 1][0]).total_seconds()
            if gap > max_gap_s:
                max_gap_s = gap

    fail_flags = [(lvl == "FAIL") for _, lvl, _, _ in items]
    consec_fail_max = _consecutive_max(fail_flags)

    # failure reasons
    fail_reason_counter = Counter()
    hard_stop_flag = False
    for _, lvl, _, fails in items:
        if lvl == "FAIL":
            for f in fails:
                fail_reason_counter[f] += 1
                if f == "STOP_FLAG_PRESENT":
                    hard_stop_flag = True

    fail_rate = (fail_count / sample_count) if sample_count > 0 else 1.0
    sample_frac = (sample_count / expected) if expected > 0 else 0.0

    reasons_warn: List[str] = []
    reasons_fail: List[str] = []

    if sample_count == 0:
        reasons_fail.append("NO_SAMPLES")
    if hard_stop_flag:
        reasons_fail.append("STOP_FLAG_PRESENT_IN_WINDOW")

    # sample coverage
    if sample_frac < args.min_sample_frac_fail:
        reasons_fail.append("LOW_SAMPLE_COVERAGE_FAIL")
    elif sample_frac < args.min_sample_frac_warn:
        reasons_warn.append("LOW_SAMPLE_COVERAGE_WARN")

    # gaps
    if max_gap_s > args.max_gap_s_fail:
        reasons_fail.append("MAX_GAP_TOO_LARGE_FAIL")
    elif max_gap_s > args.max_gap_s_warn:
        reasons_warn.append("MAX_GAP_TOO_LARGE_WARN")

    # fail rate
    if fail_rate > args.fail_rate_fail:
        reasons_fail.append("FAIL_RATE_TOO_HIGH_FAIL")
    elif fail_rate > args.fail_rate_warn:
        reasons_warn.append("FAIL_RATE_TOO_HIGH_WARN")

    # consecutive fails
    if consec_fail_max > args.consec_fail_fail:
        reasons_fail.append("CONSEC_FAIL_TOO_HIGH_FAIL")
    elif consec_fail_max > args.consec_fail_warn:
        reasons_warn.append("CONSEC_FAIL_TOO_HIGH_WARN")

    level = "PASS"
    exit_code = 0
    reason = "OK"
    if reasons_fail:
        level = "FAIL"
        exit_code = 2
        reason = reasons_fail[0]
    elif reasons_warn:
        level = "WARN"
        exit_code = 1
        reason = reasons_warn[0]

    report = {
        "schema_version": SCHEMA_VERSION,
        "ts_utc": _iso(now),
        "window": {"hours": args.window_hours, "from_utc": _iso(t_min), "to_utc": _iso(now)},
        "ok": (exit_code != 2),
        "level": level,
        "exit_code": exit_code,
        "reason": reason,
        "metrics": {
            "history_path": str(history),
            "sample_count": sample_count,
            "expected_samples": expected,
            "sample_frac": round(sample_frac, 6),
            "pass": pass_count,
            "warn": warn_count,
            "fail": fail_count,
            "fail_rate": round(fail_rate, 6),
            "max_gap_s": round(max_gap_s, 3),
            "consec_fail_max": consec_fail_max,
            "fail_reasons_top": fail_reason_counter.most_common(10),
        },
        "thresholds": {
            "expected_interval_s": args.expected_interval_s,
            "fail_rate_warn": args.fail_rate_warn,
            "fail_rate_fail": args.fail_rate_fail,
            "max_gap_s_warn": args.max_gap_s_warn,
            "max_gap_s_fail": args.max_gap_s_fail,
            "consec_fail_warn": args.consec_fail_warn,
            "consec_fail_fail": args.consec_fail_fail,
            "min_sample_frac_warn": args.min_sample_frac_warn,
            "min_sample_frac_fail": args.min_sample_frac_fail,
        },
        "reasons_warn": reasons_warn,
        "reasons_fail": reasons_fail,
    }

    try:
        out_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    except Exception:
        report.setdefault("reasons_warn", []).append("WRITE_OUT_FAILED")

    if args.archive:
        arch_dir = repo / "args" / "logs" / "soak_reports"
        arch_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        arch_path = arch_dir / f"soak_report_{stamp}_{args.window_hours}h.json"
        try:
            arch_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    sys.stdout.write(json.dumps(report, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
