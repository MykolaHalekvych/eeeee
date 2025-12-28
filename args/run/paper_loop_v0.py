from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from args.ma.policy_loader import load_policy
from args.run.csv_meta_v1 import attach_csv_meta
from args.run.run_harness_v0 import RunConfig, run_harness


@dataclass(frozen=True)
class PaperLoopConfig:
    tag: str = "paper"
    fresh_run: bool = True
    # Optional override for csv_path:
    # - can be absolute: C:\...\hg_5m_bars_ibkr.csv
    # - or repo-relative: args\data\hg_5m_bars_ibkr.csv
    csv_path: str | None = None


def _now_id() -> str:
    return uuid.uuid4().hex[:10]


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _copy_run_config_template(repo_root: Path) -> Dict[str, Any]:
    cfg_path = repo_root / "args" / "data" / "run_config_v0.json"
    # BOM-safe
    return json.loads(cfg_path.read_text(encoding="utf-8-sig"))


def _resolve_path(repo_root: Path, p: Optional[str]) -> Optional[Path]:
    if not p:
        return None
    pp = Path(str(p))
    return pp if pp.is_absolute() else (repo_root / pp)


def _choose_csv_path(repo_root: Path, rc: Dict[str, Any], cfg: PaperLoopConfig) -> Path:
    """
    Selection rule:
    1) If cfg.csv_path is set -> use it (absolute or repo-relative). If missing on disk, fallback.
    2) Else use rc["csv_path"].
    3) If chosen == sample AND hg_5m_bars_ibkr.csv exists -> switch to IBKR csv.
    4) If chosen missing -> prefer IBKR if exists, else sample if exists, else keep chosen.
    """
    data_dir = repo_root / "args" / "data"
    csv_ibkr = data_dir / "hg_5m_bars_ibkr.csv"
    csv_sample = data_dir / "hg_5m_bars_sample.csv"

    chosen = _resolve_path(repo_root, cfg.csv_path) or _resolve_path(repo_root, str(rc.get("csv_path") or ""))

    # If still None -> pick best available
    if chosen is None:
        return csv_ibkr if csv_ibkr.exists() else csv_sample

    # Prefer IBKR only when config points to sample (so we don't override intentional custom paths)
    try:
        if csv_ibkr.exists() and chosen.resolve() == csv_sample.resolve():
            chosen = csv_ibkr
    except Exception:
        if csv_ibkr.exists() and str(chosen).lower().endswith("hg_5m_bars_sample.csv"):
            chosen = csv_ibkr

    # If chosen doesn't exist -> fallback to best available
    if not chosen.exists():
        if csv_ibkr.exists():
            return csv_ibkr
        if csv_sample.exists():
            return csv_sample

    return chosen


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        f.write("\n")


def _evt_kind(ev: Dict[str, Any]) -> str:
    for k in ("kind", "type", "event_type", "name"):
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return ""


def _dg(d: Any, path: str, default=None):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _extract_rule_ids(violations: Any, limit: int = 12) -> List[str]:
    out: List[str] = []
    if isinstance(violations, list):
        for x in violations:
            if not isinstance(x, dict):
                continue
            rid = x.get("rule_id")
            if isinstance(rid, str) and rid.strip():
                out.append(rid.strip())
            if len(out) >= limit:
                break
    return out


def _extract_position_size(ev: Dict[str, Any]) -> float:
    # Prefer risk_envelope.position_size if present (single source-of-truth from MA runtime).
    re = ev.get("risk_envelope")
    if isinstance(re, dict):
        v = re.get("position_size")
        if isinstance(v, (int, float)):
            return float(v)

    ps = ev.get("position_state")
    if isinstance(ps, dict):
        v2 = ps.get("size")
        if isinstance(v2, (int, float)):
            return float(v2)

    return 0.0


def _compute_gate_reason(
    *,
    enforced_no_trade: bool,
    mode: str,
    has_position: bool,
    ma_decision: str,
    rule_ids: List[str],
) -> Optional[str]:
    """
    gate_reason is used to explain INTENT_NONE when the system is effectively prevented from acting.
    If we are "allowed" but still do nothing (e.g., no strategy signal), gate_reason should remain None.
    """
    base: Optional[str] = None

    if enforced_no_trade:
        base = "ENFORCED_NO_TRADE"
    elif mode == "NO_TRADE":
        base = "MODE_NO_TRADE"
    elif mode == "ONLY_EXITS" and (not has_position):
        base = "ONLY_EXITS_NO_POSITION"
    elif ma_decision and ma_decision != "ALLOW":
        base = f"MA_{ma_decision}"
    else:
        base = None

    if base is None:
        return None

    if rule_ids:
        return base + ":" + ",".join(rule_ids[:8])
    return base


def _build_order_intent(
    *,
    run_id: str,
    tick_ev: Dict[str, Any],
    instrument: str,
    timeframe: str,
    env: str,
    operator_global_mode: Optional[str],
) -> Dict[str, Any]:
    re = tick_ev.get("risk_envelope") if isinstance(tick_ev.get("risk_envelope"), dict) else {}
    mi = tick_ev.get("ma_input") if isinstance(tick_ev.get("ma_input"), dict) else {}

    mode = str(re.get("mode") or "").strip().upper() or "UNKNOWN"
    enforced_no_trade = bool(re.get("enforced_no_trade", False))

    # Observability echo (Stage 5.8+)
    exec_global_mode = (
        str(re.get("exec_global_mode") or "").strip().upper()
        or str(_dg(mi, "exec.global_mode", "") or "").strip().upper()
        or None
    )

    ma_decision = str(tick_ev.get("ma_decision") or "").strip().upper() or "UNKNOWN"
    violations = tick_ev.get("violations", [])
    rule_ids = _extract_rule_ids(violations)

    pos_size = _extract_position_size(tick_ev)
    has_position = abs(pos_size) > 1e-12

    # Capability flags derived from risk_envelope.mode (single source-of-truth)
    allow_new_entries = (not enforced_no_trade) and (mode == "ALLOW_NEW_ENTRIES") and (ma_decision == "ALLOW")
    allow_exits = (not enforced_no_trade) and (mode in {"ONLY_EXITS", "ALLOW_NEW_ENTRIES"}) and has_position

    # Step 6 kickoff: we do not have a strategy signal here, so we emit INTENT_NONE,
    # but we make the gating explicit and future-proof.
    kind = "INTENT_NONE"
    kind_raw = "INTENT_NONE"

    gate_reason = _compute_gate_reason(
        enforced_no_trade=enforced_no_trade,
        mode=mode,
        has_position=has_position,
        ma_decision=ma_decision,
        rule_ids=rule_ids,
    )

    note: Optional[str] = None
    if gate_reason is None and (not allow_new_entries) and mode == "ALLOW_NEW_ENTRIES":
        # This means mode allows entries, but we still emit NONE (no strategy).
        note = "ALLOW_NEW_ENTRIES_BUT_NO_STRATEGY"

    intent: Dict[str, Any] = {
        "schema_version": "order_intent_v0",
        "run_id": run_id,
        "index": tick_ev.get("index"),
        "ts": tick_ev.get("ts"),
        "instrument": instrument,
        "timeframe": timeframe,
        "env": env,
        "kind": kind,
        "kind_raw": kind_raw,
        "gate_reason": gate_reason,
        # Traceability / observability
        "operator_global_mode": operator_global_mode,
        "exec_global_mode": exec_global_mode,
        "mode": mode,
        "mode_source": re.get("mode_source"),
        "enforced_no_trade": enforced_no_trade,
        "ma_decision": ma_decision,
        "has_position": has_position,
        "position_size": int(pos_size) if float(pos_size).is_integer() else pos_size,
        "rule_ids": rule_ids,
        # Capability flags (what is permitted right now)
        "allow_new_entries": allow_new_entries,
        "allow_exits": allow_exits,
    }
    if note:
        intent["note"] = note

    return intent


def run_paper_loop(cfg: PaperLoopConfig) -> Dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    run_id = _now_id()

    # Load run_config_v0.json (template)
    rc = _copy_run_config_template(repo_root)

    # Build per-run event/output paths
    data_dir = repo_root / "args" / "data"
    out_events = data_dir / f"events_run_{run_id}.jsonl"
    out_orders = data_dir / f"orders_paper_{run_id}.jsonl"

    # Step 6 artifact
    out_intents = data_dir / f"order_intents_{run_id}.jsonl"

    # Resolve inputs
    csv_path = _choose_csv_path(repo_root, rc, cfg)
    policy_path = (
        _resolve_path(repo_root, str(rc.get("policy_path") or ""))
        or (repo_root / "args" / "data" / "invariants_hg_v0.yaml")
    )

    max_steps = int(rc.get("max_steps", 0))
    start_index = int(rc.get("start_index", 0))
    defaults = rc.get("defaults", {}) if isinstance(rc.get("defaults", {}), dict) else {}

    # Fresh-run: ensure output files start empty
    if cfg.fresh_run:
        for p in (out_events, out_orders, out_intents):
            if p.exists():
                p.unlink()

    # Load policy meta (for report visibility)
    pol = load_policy(str(policy_path))

    # 1) Run harness (writes events_run_<run_id>.jsonl)
    hcfg = RunConfig(
        csv_path=csv_path,
        policy_path=policy_path,
        out_events=out_events,
        max_steps=max_steps,
        start_index=start_index,
        defaults=defaults,
    )
    h_summary = run_harness(hcfg)

    # 2) Step 6 kickoff + WA v0 stub:
    #    - Read harness events, rewrite events file with ORDER_INTENT inserted after each TICK
    #    - Write order_intents_<run_id>.jsonl (one per tick)
    #    - Write orders_paper_<run_id>.jsonl (one per tick, WA stub)
    from args.wa.wa_v0_stub import decide_wa_action  # local import to avoid cycles

    n_ticks = 0
    n_orders = 0
    n_intents = 0
    parse_seen = 0
    parse_errors = 0

    intent_kind_counts: Dict[str, int] = defaultdict(int)
    gate_reason_counts: Dict[str, int] = defaultdict(int)

    # Capture last tick observability for mode-gate source-of-truth
    last_tick_index = None
    last_tick_ts = None
    last_ma_decision = None
    last_risk_envelope: Dict[str, Any] = {}
    last_position_state: Dict[str, Any] = {}

    # Operator intent (echo from RUN_START if present)
    operator_global_mode: Optional[str] = None

    if not out_events.exists():
        raise FileNotFoundError(f"harness did not produce events file: {out_events}")

    tmp_events = out_events.with_suffix(".jsonl.tmp")
    if tmp_events.exists():
        tmp_events.unlink()

    try:
        with out_events.open("r", encoding="utf-8-sig", errors="replace") as fin, tmp_events.open(
            "w", encoding="utf-8"
        ) as fout:
            for raw in fin:
                s = raw.strip()
                if not s:
                    continue

                parse_seen += 1
                try:
                    ev = json.loads(s)
                except Exception:
                    parse_errors += 1
                    fout.write(raw if raw.endswith("\n") else (raw + "\n"))
                    continue

                if not isinstance(ev, dict):
                    fout.write(raw if raw.endswith("\n") else (raw + "\n"))
                    continue

                # Preserve original event line as-is
                fout.write(raw if raw.endswith("\n") else (raw + "\n"))

                k = _evt_kind(ev)

                if k == "RUN_START":
                    # Capture operator intent echo for later intents
                    cs = ev.get("control_state")
                    if isinstance(cs, dict):
                        gm = cs.get("global_mode")
                        if isinstance(gm, str) and gm.strip():
                            operator_global_mode = gm.strip().upper()

                if k != "TICK":
                    continue

                n_ticks += 1

                # --- WA v0 stub (paper orders log) ---
                ma_decision = ev.get("ma_decision")
                violations = ev.get("violations", [])
                if not isinstance(violations, list):
                    violations = []

                action = decide_wa_action(ma_decision, violations)
                order = {
                    "kind": "ORDER_PAPER",
                    "source": "WA_V0",
                    "run_id": run_id,
                    "index": ev.get("index"),
                    "ts": ev.get("ts"),
                    "instrument": str(defaults.get("instrument", pol.instrument)),
                    "timeframe": str(defaults.get("timeframe", pol.timeframe)),
                    "ma_decision": str(ma_decision or "UNKNOWN"),
                    "wa_action": action.to_dict(),
                }
                _append_jsonl(out_orders, order)
                n_orders += 1

                # --- Step 6: order intent (gated by MA risk_envelope.mode) ---
                intent = _build_order_intent(
                    run_id=run_id,
                    tick_ev=ev,
                    instrument=str(defaults.get("instrument", pol.instrument)),
                    timeframe=str(defaults.get("timeframe", pol.timeframe)),
                    env=str(defaults.get("env", pol.environment)),
                    operator_global_mode=operator_global_mode,
                )

                # Invariant: never allow entry intent unless ALLOW_NEW_ENTRIES and not enforced.
                # (Kickoff emits INTENT_NONE, but keep invariant for future strategy upgrades.)
                if str(intent.get("kind") or "").strip().upper() in {"INTENT_ENTRY", "INTENT_NEW_ENTRY"}:
                    if not bool(intent.get("allow_new_entries", False)):
                        intent["kind"] = "INTENT_NONE"
                        intent["kind_raw"] = "INTENT_ENTRY_GATED"
                        if not intent.get("gate_reason"):
                            intent["gate_reason"] = "ENTRY_GATED_BY_MODE_OR_ENFORCEMENT"

                _append_jsonl(out_intents, intent)
                n_intents += 1

                ik = str(intent.get("kind") or "UNKNOWN").strip().upper() or "UNKNOWN"
                intent_kind_counts[ik] += 1
                gr = intent.get("gate_reason")
                if isinstance(gr, str) and gr.strip():
                    gate_reason_counts[gr.strip()] += 1

                # Also emit ORDER_INTENT event into events stream for UI Step 6 counters.
                # IMPORTANT: do NOT include top-level ma_decision/decision/action/result keys,
                # otherwise the "decision histogram" will be polluted by non-TICK events.
                intent_evt = {
                    "kind": "ORDER_INTENT",
                    "run_id": run_id,
                    "index": ev.get("index"),
                    "ts": ev.get("ts"),
                    "intent": intent,
                    "risk_envelope": ev.get("risk_envelope"),
                    "position_state": ev.get("position_state"),
                }
                fout.write(json.dumps(intent_evt, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                fout.write("\n")

                # Update last tick snapshot for report
                last_tick_index = ev.get("index")
                last_tick_ts = ev.get("ts")
                last_ma_decision = ma_decision

                re = ev.get("risk_envelope")
                if isinstance(re, dict):
                    last_risk_envelope = dict(re)

                ps = ev.get("position_state")
                if isinstance(ps, dict):
                    last_position_state = dict(ps)

        # Replace original events with enriched version
        tmp_events.replace(out_events)

    finally:
        # Best-effort cleanup if something went wrong mid-way
        if tmp_events.exists():
            try:
                tmp_events.unlink()
            except Exception:
                pass

    # 3) Report (write into logs)
    logs_dir = repo_root / "args" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    report_path = logs_dir / f"run_report_{run_id}_{cfg.tag}.json"

    top_gate_reasons = sorted(gate_reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    report: Dict[str, Any] = {
        "run_id": run_id,
        "tag": cfg.tag,
        "fresh_run": cfg.fresh_run,
        "report_path": str(report_path),
        # Policy identity (for audit)
        "policy_name": pol.name,
        "schema_version": pol.schema_version,
        # Canonical identity fields
        "instrument": str(defaults.get("instrument", pol.instrument)),
        "timeframe": str(defaults.get("timeframe", pol.timeframe)),
        "env": str(defaults.get("env", pol.environment)),
        "inputs": {
            "csv_path": str(csv_path),
            "policy_path": str(policy_path),
        },
        "outputs": {
            "events_run": str(out_events),
            "orders_paper": str(out_orders),
            "order_intents": str(out_intents),
        },
        "harness_summary": h_summary,
        "wa_summary": {
            "ticks": n_ticks,
            "orders_written": n_orders,
            "intents_written": n_intents,
            "events_parse_seen": parse_seen,
            "events_parse_errors": parse_errors,
            "intent_kind_counts": dict(sorted(intent_kind_counts.items(), key=lambda kv: kv[0])),
            "top_gate_reasons": top_gate_reasons,
        },
        # Stage D: source-of-truth for mode gating (from last TICK)
        "last_tick": {
            "index": last_tick_index,
            "ts": last_tick_ts,
            "ma_decision": last_ma_decision,
        },
        "risk_envelope": last_risk_envelope,
        "position_state": last_position_state,
    }

    # Attach CSV meta (if <csv>.meta.json exists)
    attach_csv_meta(report, csv_path)

    # Persist report
    _write_json(report_path, report)

    return report


def main() -> int:
    cfg = PaperLoopConfig(tag="paper", fresh_run=True)
    report = run_paper_loop(cfg)

    # Compact summary
    out = report.get("outputs", {})
    h = report.get("harness_summary", {})
    w = report.get("wa_summary", {})

    print("PAPER_LOOP")
    print("csv_path:", report.get("inputs", {}).get("csv_path"))
    print("run_id:", report.get("run_id"))
    print("events_run:", out.get("events_run"))
    print("orders_paper:", out.get("orders_paper"))
    print("order_intents:", out.get("order_intents"))
    print("report_path:", report.get("report_path"))
    print("harness_processed:", h.get("processed"), "decisions:", h.get("ma_decisions"))
    print(
        "wa_ticks:",
        w.get("ticks"),
        "orders_written:",
        w.get("orders_written"),
        "intents_written:",
        w.get("intents_written"),
    )
    # Stage D quick visibility:
    re = report.get("risk_envelope") if isinstance(report.get("risk_envelope"), dict) else {}
    print("mode:", re.get("mode"), "enforced_no_trade:", re.get("enforced_no_trade"))
    ps = report.get("position_state") if isinstance(report.get("position_state"), dict) else {}
    print("position_size:", ps.get("size"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
