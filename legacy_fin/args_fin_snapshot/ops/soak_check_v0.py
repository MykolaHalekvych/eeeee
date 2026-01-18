from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

OPS_EVENTS = LOGS_DIR / "ops_events.jsonl"
SOAK_REPORT = DATA_DIR / "soak_report.json"


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            continue
    return out


@dataclass
class CycleAgg:
    cycle_id: str
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    run_id: Optional[str] = None
    ok: Optional[bool] = None
    steps: Dict[str, Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.steps is None:
            self.steps = {}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser("soak_check_v0")
    ap.add_argument(
        "--last", type=int, default=50, help="How many last cycles to evaluate"
    )
    ap.add_argument("--max_failures", type=int, default=0, help="Allowed failed cycles")
    ap.add_argument(
        "--max_timeouts", type=int, default=0, help="Allowed TIMEOUT step results"
    )
    ap.add_argument(
        "--require_run_change",
        action="store_true",
        help="Require run_id to change across cycles",
    )
    args = ap.parse_args(argv)

    events = _read_jsonl(OPS_EVENTS)
    if not events:
        rep = {"ok": False, "reason": "NO_EVENTS", "path": str(OPS_EVENTS)}
        SOAK_REPORT.parent.mkdir(parents=True, exist_ok=True)
        SOAK_REPORT.write_text(
            json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 2

    cycles: Dict[str, CycleAgg] = {}

    for ev in events:
        if ev.get("kind") != "OPS_EVENT":
            continue
        cid = str(ev.get("cycle_id") or "")
        if not cid:
            continue
        c = cycles.get(cid)
        if c is None:
            c = CycleAgg(cycle_id=cid)
            cycles[cid] = c

        et = str(ev.get("event") or "")
        if et == "CYCLE_START":
            c.start_ts = str(ev.get("ts") or "")
            c.run_id = ev.get("run_id") or c.run_id
        elif et == "CYCLE_END":
            c.end_ts = str(ev.get("ts") or "")
            c.ok = bool(ev.get("ok")) if ev.get("ok") is not None else c.ok
        elif et == "STEP_END":
            step = str(ev.get("step") or "")
            if step:
                c.steps[step] = {
                    "status": ev.get("status"),
                    "rc": ev.get("rc"),
                    "log_path": ev.get("log_path"),
                }

    # sort cycles by start_ts lexicographically (ISO Z)
    ordered = sorted(cycles.values(), key=lambda x: (x.start_ts or ""), reverse=True)
    last_n = ordered[: max(1, int(args.last))]

    failures = 0
    timeouts = 0
    step_errors = 0
    run_ids = []

    for c in last_n:
        run_ids.append(c.run_id)
        if c.ok is False:
            failures += 1
        for sname, sr in (c.steps or {}).items():
            st = str(sr.get("status") or "")
            if st == "TIMEOUT":
                timeouts += 1
            if st in {"ERROR", "TIMEOUT"}:
                step_errors += 1

    run_change_ok = True
    if args.require_run_change:
        # require at least 2 distinct non-empty run_ids across the evaluated window
        uniq = {r for r in run_ids if isinstance(r, str) and r}
        run_change_ok = len(uniq) >= 2

    ok = (
        (failures <= args.max_failures)
        and (timeouts <= args.max_timeouts)
        and run_change_ok
    )

    rep = {
        "schema": "soak_check_v0",
        "ok": bool(ok),
        "window_cycles": len(last_n),
        "failures": failures,
        "timeouts": timeouts,
        "step_errors": step_errors,
        "require_run_change": bool(args.require_run_change),
        "run_change_ok": bool(run_change_ok),
        "latest_cycle": {
            "cycle_id": last_n[0].cycle_id if last_n else None,
            "start_ts": last_n[0].start_ts if last_n else None,
            "end_ts": last_n[0].end_ts if last_n else None,
            "run_id": last_n[0].run_id if last_n else None,
            "ok": last_n[0].ok if last_n else None,
        },
        "events_path": str(OPS_EVENTS),
    }

    SOAK_REPORT.parent.mkdir(parents=True, exist_ok=True)
    SOAK_REPORT.write_text(
        json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
