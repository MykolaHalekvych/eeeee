
# args/run/paper_loop_v0.py
from __future__ import annotations

import json
import subprocess
import sys
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

    # 5A safety-by-default:
    # execute=False MUST NOT send anything to IBKR.
    execute: bool = False

    # 5A artifacts:
    gen_payload: bool = True
    gen_sendplan: bool = True

    # Optional override for csv_path:
    # - can be absolute: C:\...\hg_5m_bars_ibkr.csv
    # - or repo-relative: args\data\hg_5m_bars_ibkr.csv
    csv_path: str | None = None


# ---------------------------
# small utilities
# ---------------------------

def _now_id() -> str:
    return uuid.uuid4().hex[:10]


def _json_compact(obj: Dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl_line(f, obj: Dict[str, Any]) -> None:
    f.write(_json_compact(obj))
    f.write("\n")


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


def _as_int(x: Any) -> Optional[int]:
    if isinstance(x, int):
        return x
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return None
        try:
            return int(s)
        except Exception:
            return None
    return None


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
    gate_reason explains INTENT_NONE when the system is effectively prevented from acting.
    If we are "allowed" but still do nothing (e.g., no strategy signal), gate_reason stays None.
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

    # Observability echo
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

    # Kickoff: no strategy signal here -> INTENT_NONE
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
        # Capability flags
        "allow_new_entries": allow_new_entries,
        "allow_exits": allow_exits,
    }
    if note:
        intent["note"] = note

    return intent


def _run_module_json(repo_root: Path, module: str, args: List[str]) -> Dict[str, Any]:
    """
    Runs: <python> -m <module> <args...>
    Returns diagnostics + best-effort parsed JSON from stdout (if module prints a JSON object).
    """
    cmd = [sys.executable, "-m", module, *args]
    cp = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)

    out = (cp.stdout or "").strip()
    err = (cp.stderr or "").strip()

    parsed: Optional[Dict[str, Any]] = None
    if out:
        i = out.find("{")
        j = out.rfind("}")
        if i != -1 and j != -1 and j > i:
            try:
                parsed_obj = json.loads(out[i : j + 1])
                if isinstance(parsed_obj, dict):
                    parsed = parsed_obj
            except Exception:
                parsed = None

    return {
        "ok": (cp.returncode == 0),
        "returncode": cp.returncode,
        "cmd": cmd,
        "stdout_tail": out[-4000:],
        "stderr_tail": err[-4000:],
        "parsed": parsed,
    }


def _count_jsonl_field(path: Path, field: str) -> Dict[str, Any]:
    """
    Counts distinct values of a field in a JSONL file.
    """
    seen = 0
    errors = 0
    counts: Dict[str, int] = defaultdict(int)

    if not path.exists():
        return {"path": str(path), "seen": 0, "errors": 0, "counts": {}}

    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            seen += 1
            try:
                obj = json.loads(s)
            except Exception:
                errors += 1
                continue
            if not isinstance(obj, dict):
                continue
            v = obj.get(field)
            if isinstance(v, str) and v.strip():
                counts[v.strip().upper()] += 1
            else:
                counts["(MISSING)"] += 1

    return {"path": str(path), "seen": seen, "errors": errors, "counts": dict(counts)}


def _load_sendplan_by_index(sendplan_path: Path) -> Dict[int, List[Dict[str, Any]]]:
    """
    Index -> list of sendplan rows (future-proof: multiple sends per tick).
    """
    mp: Dict[int, List[Dict[str, Any]]] = defaultdict(list)

    if not sendplan_path.exists():
        return mp

    with sendplan_path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            idx = _as_int(obj.get("index"))
            if idx is None:
                continue
            mp[idx].append(obj)

    return mp


def _inject_order_submit_events(
    *,
    events_path: Path,
    sendplan_path: Path,
    run_id: str,
    execute_default: bool,
) -> Dict[str, Any]:
    """
    Standard 5A requirement:
      - events_run_<rid>.jsonl MUST include ORDER_SUBMIT events (execute=false in 5A).
    Implementation:
      - Rewrite events file and inject ORDER_SUBMIT after each ORDER_INTENT
      - Remove any existing ORDER_SUBMIT to avoid duplicates.
    """
    if not events_path.exists():
        raise FileNotFoundError(f"events_run not found: {events_path}")
    if not sendplan_path.exists():
        # no sendplan => cannot inject
        return {"ok": True, "inserted": 0, "plans_total": 0, "unmatched_plans": 0}

    by_index = _load_sendplan_by_index(sendplan_path)
    plans_total = sum(len(v) for v in by_index.values())

    tmp = events_path.with_suffix(".jsonl.tmp_submit")
    if tmp.exists():
        tmp.unlink()

    inserted = 0
    matched_plan_ids: set[str] = set()

    def _pid(x: Any) -> Optional[str]:
        if isinstance(x, str) and x.strip():
            return x.strip()
        return None

    try:
        with events_path.open("r", encoding="utf-8-sig", errors="replace") as fin, tmp.open("w", encoding="utf-8") as fout:
            for raw in fin:
                s = raw.strip()
                if not s:
                    continue

                try:
                    ev = json.loads(s)
                except Exception:
                    # keep raw
                    fout.write(raw if raw.endswith("\n") else (raw + "\n"))
                    continue

                if not isinstance(ev, dict):
                    fout.write(raw if raw.endswith("\n") else (raw + "\n"))
                    continue

                k = _evt_kind(ev)
                if k == "ORDER_SUBMIT":
                    # drop old submits (idempotent rewrite)
                    continue

                # write original line back
                fout.write(raw if raw.endswith("\n") else (raw + "\n"))

                if k != "ORDER_INTENT":
                    continue

                idx = _as_int(ev.get("index"))
                if idx is None:
                    continue

                plans = by_index.get(idx, [])
                if not plans:
                    continue

                for plan in plans:
                    # Minimal, safe top-level fields (no ma_decision/decision pollution)
                    submit_evt: Dict[str, Any] = {
                        "kind": "ORDER_SUBMIT",
                        "run_id": run_id,
                        "index": plan.get("index"),
                        "ts": plan.get("ts") or ev.get("ts"),
                        "execute": bool(plan.get("execute", execute_default)),
                        "payload_execute": bool(plan.get("payload_execute", execute_default)) if "payload_execute" in plan else bool(plan.get("execute", execute_default)),
                        "plan_kind": plan.get("plan_kind"),
                        "payload_kind": plan.get("payload_kind"),
                        "payload_id": plan.get("payload_id"),
                        "plan_id": plan.get("plan_id"),
                        "reason": plan.get("reason"),
                        "gate_reason": plan.get("gate_reason"),
                        "rule_ids": plan.get("rule_ids"),
                        "env": plan.get("env"),
                        "instrument": plan.get("instrument"),
                        "timeframe": plan.get("timeframe"),
                    }
                    fout.write(_json_compact(submit_evt))
                    fout.write("\n")
                    inserted += 1

                    pid = _pid(plan.get("plan_id")) or _pid(plan.get("payload_id"))
                    if pid:
                        matched_plan_ids.add(pid)

        tmp.replace(events_path)

    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

    # best-effort unmatched estimate:
    # if plan_id/payload_id missing everywhere, unmatched_plans will be 0 (we cannot know better)
    unmatched_plans = 0
    if plans_total > 0:
        # if we inserted less than total, unmatched exists
        unmatched_plans = max(0, plans_total - inserted)

    return {"ok": True, "inserted": inserted, "plans_total": plans_total, "unmatched_plans": unmatched_plans}


# ---------------------------
# main pipeline
# ---------------------------

def run_paper_loop(cfg: PaperLoopConfig) -> Dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    run_id = _now_id()

    # Load run_config_v0.json (template)
    rc = _copy_run_config_template(repo_root)

    # Build per-run paths
    data_dir = repo_root / "args" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    out_events = data_dir / f"events_run_{run_id}.jsonl"
    out_orders = data_dir / f"orders_paper_{run_id}.jsonl"

    # Artifacts for 5A
    out_intents = data_dir / f"order_intents_{run_id}.jsonl"
    out_payload = data_dir / f"orders_payload_{run_id}.jsonl"
    out_sendplan = data_dir / f"orders_sendplan_{run_id}.jsonl"

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
        for p in (out_events, out_orders, out_intents, out_payload, out_sendplan):
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

    # 2) Build ORDER_INTENT (gated by MA) + WA v0 stub paper orders
    from args.wa.wa_v0_stub import decide_wa_action  # local import to avoid cycles

    n_ticks = 0
    n_orders = 0
    n_intents = 0
    parse_seen = 0
    parse_errors = 0

    intent_kind_counts: Dict[str, int] = defaultdict(int)
    gate_reason_counts: Dict[str, int] = defaultdict(int)

    # Capture last tick observability
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
        with (
            out_events.open("r", encoding="utf-8-sig", errors="replace") as fin,
            tmp_events.open("w", encoding="utf-8") as fout,
            out_orders.open("a", encoding="utf-8") as forders,
            out_intents.open("a", encoding="utf-8") as fintents,
        ):
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

                # preserve original event
                fout.write(raw if raw.endswith("\n") else (raw + "\n"))

                k = _evt_kind(ev)

                if k == "RUN_START":
                    cs = ev.get("control_state")
                    if isinstance(cs, dict):
                        gm = cs.get("global_mode")
                        if isinstance(gm, str) and gm.strip():
                            operator_global_mode = gm.strip().upper()

                if k != "TICK":
                    continue

                n_ticks += 1

                # WA v0 stub (paper orders log)
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
                _write_jsonl_line(forders, order)
                n_orders += 1

                # ORDER_INTENT
                intent = _build_order_intent(
                    run_id=run_id,
                    tick_ev=ev,
                    instrument=str(defaults.get("instrument", pol.instrument)),
                    timeframe=str(defaults.get("timeframe", pol.timeframe)),
                    env=str(defaults.get("env", pol.environment)),
                    operator_global_mode=operator_global_mode,
                )

                # Future-proof invariant (even though kickoff emits INTENT_NONE)
                if str(intent.get("kind") or "").strip().upper() in {"INTENT_ENTRY", "INTENT_NEW_ENTRY"}:
                    if not bool(intent.get("allow_new_entries", False)):
                        intent["kind"] = "INTENT_NONE"
                        intent["kind_raw"] = "INTENT_ENTRY_GATED"
                        if not intent.get("gate_reason"):
                            intent["gate_reason"] = "ENTRY_GATED_BY_MODE_OR_ENFORCEMENT"

                _write_jsonl_line(fintents, intent)
                n_intents += 1

                ik = str(intent.get("kind") or "UNKNOWN").strip().upper() or "UNKNOWN"
                intent_kind_counts[ik] += 1
                gr = intent.get("gate_reason")
                if isinstance(gr, str) and gr.strip():
                    gate_reason_counts[gr.strip()] += 1

                # Emit ORDER_INTENT into events stream
                intent_evt = {
                    "kind": "ORDER_INTENT",
                    "run_id": run_id,
                    "index": ev.get("index"),
                    "ts": ev.get("ts"),
                    "intent": intent,
                    "risk_envelope": ev.get("risk_envelope"),
                    "position_state": ev.get("position_state"),
                }
                fout.write(_json_compact(intent_evt))
                fout.write("\n")

                # last tick snapshot
                last_tick_index = ev.get("index")
                last_tick_ts = ev.get("ts")
                last_ma_decision = ma_decision

                re = ev.get("risk_envelope")
                if isinstance(re, dict):
                    last_risk_envelope = dict(re)

                ps = ev.get("position_state")
                if isinstance(ps, dict):
                    last_position_state = dict(ps)

        tmp_events.replace(out_events)

    finally:
        if tmp_events.exists():
            try:
                tmp_events.unlink()
            except Exception:
                pass

    # Ensure artifacts exist even if 0 ticks
    if not out_orders.exists():
        out_orders.touch()
    if not out_intents.exists():
        out_intents.touch()

    # 5A: payload + sendplan (execute flag flows, but paper_loop itself does NOT send anything)
    payload_module_run: Dict[str, Any] = {}
    sendplan_module_run: Dict[str, Any] = {}

    if cfg.gen_payload:
        payload_module_run = _run_module_json(
            repo_root,
            "args.wa.order_payload_v0",
            ["--intents", str(out_intents), "--execute", ("1" if cfg.execute else "0")],
        )
        if not payload_module_run.get("ok", False):
            raise RuntimeError(
                "order_payload_v0 failed\n"
                + f"cmd={payload_module_run.get('cmd')}\n"
                + f"stdout_tail={payload_module_run.get('stdout_tail')}\n"
                + f"stderr_tail={payload_module_run.get('stderr_tail')}\n"
            )

        p = payload_module_run.get("parsed") if isinstance(payload_module_run.get("parsed"), dict) else {}
        if isinstance(p.get("out_path"), str) and p.get("out_path"):
            out_payload = Path(p["out_path"])

    if cfg.gen_sendplan:
        if not out_payload.exists():
            raise FileNotFoundError(f"orders_payload not found: {out_payload}")

        sendplan_module_run = _run_module_json(
            repo_root,
            "args.wa.order_sendplan_v0",
            ["--payload", str(out_payload), "--execute", ("1" if cfg.execute else "0")],
        )
        if not sendplan_module_run.get("ok", False):
            raise RuntimeError(
                "order_sendplan_v0 failed\n"
                + f"cmd={sendplan_module_run.get('cmd')}\n"
                + f"stdout_tail={sendplan_module_run.get('stdout_tail')}\n"
                + f"stderr_tail={sendplan_module_run.get('stderr_tail')}\n"
            )

        sp = sendplan_module_run.get("parsed") if isinstance(sendplan_module_run.get("parsed"), dict) else {}
        if isinstance(sp.get("out_path"), str) and sp.get("out_path"):
            out_sendplan = Path(sp["out_path"])

    # 5A required: ORDER_SUBMIT events (execute=false in 5A)
    order_submit_inject = _inject_order_submit_events(
        events_path=out_events,
        sendplan_path=out_sendplan,
        run_id=run_id,
        execute_default=cfg.execute,
    )

    # Summaries (scan JSONL)
    payload_kind_summary = _count_jsonl_field(out_payload, "payload_kind") if out_payload.exists() else {}
    sendplan_kind_summary = _count_jsonl_field(out_sendplan, "plan_kind") if out_sendplan.exists() else {}

    # Report
    logs_dir = repo_root / "args" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    report_path = logs_dir / f"run_report_{run_id}_{cfg.tag}.json"

    top_gate_reasons = sorted(gate_reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:10]

    report: Dict[str, Any] = {
        "run_id": run_id,
        "tag": cfg.tag,
        "fresh_run": cfg.fresh_run,
        "execute": cfg.execute,
        "report_path": str(report_path),

        "policy_name": pol.name,
        "schema_version": pol.schema_version,

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
            "orders_payload": str(out_payload),
            "orders_sendplan": str(out_sendplan),
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

            "payload_module": payload_module_run.get("parsed") or {"ok": payload_module_run.get("ok", False)},
            "sendplan_module": sendplan_module_run.get("parsed") or {"ok": sendplan_module_run.get("ok", False)},

            "payload_kind_counts": payload_kind_summary,
            "sendplan_kind_counts": sendplan_kind_summary,

            "order_submit_inject": order_submit_inject,
            "order_submit_events": int(order_submit_inject.get("inserted", 0)),
        },

        "last_tick": {
            "index": last_tick_index,
            "ts": last_tick_ts,
            "ma_decision": last_ma_decision,
        },
        "risk_envelope": last_risk_envelope,
        "position_state": last_position_state,
    }

    attach_csv_meta(report, csv_path)
    _write_json(report_path, report)
    return report


def main() -> int:
    cfg = PaperLoopConfig(tag="paper", fresh_run=True, execute=False)
    report = run_paper_loop(cfg)

    out = report.get("outputs", {})
    h = report.get("harness_summary", {})
    w = report.get("wa_summary", {})

    print("PAPER_LOOP")
    print("csv_path:", report.get("inputs", {}).get("csv_path"))
    print("run_id:", report.get("run_id"))
    print("events_run:", out.get("events_run"))
    print("orders_paper:", out.get("orders_paper"))
    print("order_intents:", out.get("order_intents"))
    print("orders_payload:", out.get("orders_payload"))
    print("orders_sendplan:", out.get("orders_sendplan"))
    print("report_path:", report.get("report_path"))
    print("harness_processed:", h.get("processed"), "decisions:", h.get("ma_decisions"))
    print(
        "wa_ticks:",
        w.get("ticks"),
        "orders_written:",
        w.get("orders_written"),
        "intents_written:",
        w.get("intents_written"),
        "order_submit_events:",
        w.get("order_submit_events"),
    )
    re = report.get("risk_envelope") if isinstance(report.get("risk_envelope"), dict) else {}
    print("mode:", re.get("mode"), "enforced_no_trade:", re.get("enforced_no_trade"))
    ps = report.get("position_state") if isinstance(report.get("position_state"), dict) else {}
    print("position_size:", ps.get("size"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
