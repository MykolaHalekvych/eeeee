from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# File: args/ops/auto_loop_v1.py
#
# Stage 7 Ops loop:
# - single-instance lock with TTL + heartbeat
# - stop.flag respected everywhere in the loop
# - JSON health summary (args/data/ops_health.json)
# - JSONL event stream (args/logs/ops_events.jsonl)
#
# Safe-by-default:
# - will NOT run unless control_state.ops_loop.enabled==true, or --force is given.
# - lock stealing is OFF unless explicitly enabled (cfg steal_stale_lock or --steal-stale-lock)


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"
LOGS_DIR = REPO_ROOT / "args" / "logs"

LOCK_PATH = DATA_DIR / "auto_loop.lock.json"
STOP_FLAG_PATH = DATA_DIR / "stop.flag"
OPS_HEALTH_PATH = DATA_DIR / "ops_health.json"
OPS_EVENTS_PATH = LOGS_DIR / "ops_events.jsonl"


def _utc_now_z() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc_ts(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s.strip():
        return None
    ss = s.strip()
    if ss.endswith("Z"):
        ss = ss[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ss)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _safe_name(s: str, max_len: int = 80) -> str:
    x = re.sub(r"[^A-Za-z0-9_.-]+", "_", (s or "").strip())
    x = x.strip("._-")
    return x[:max_len] if x else "step"


def _read_json_dict(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _latest_run_report_paper() -> Optional[Path]:
    if not LOGS_DIR.exists():
        return None
    cand: List[Path] = []
    for p in LOGS_DIR.iterdir():
        if p.is_file() and p.name.startswith("run_report_") and p.name.endswith("_paper.json"):
            cand.append(p)
    if not cand:
        return None
    cand.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return cand[0]


def _run_id_from_report_path(p: Path) -> Optional[str]:
    m = re.match(r"^run_report_([A-Za-z0-9]+)_paper\.json$", p.name)
    return m.group(1) if m else None


def _as_bool(x: Any, default: bool = False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, int):
        return bool(x)
    if isinstance(x, str):
        s = x.strip().lower()
        if s in {"true", "1", "yes", "y", "on"}:
            return True
        if s in {"false", "0", "no", "n", "off"}:
            return False
    return default


def _as_int(x: Any, default: int, *, min_v: int = 0, max_v: int = 10**9) -> int:
    try:
        v = int(x)
    except Exception:
        return default
    if v < min_v:
        return min_v
    if v > max_v:
        return max_v
    return v


@dataclass
class StepCfg:
    name: str
    cmd: List[str]
    timeout_s: int
    allow_fail: bool = False
    cwd: Optional[str] = None


@dataclass
class OpsCfg:
    enabled: bool = False
    once: bool = False
    interval_s: int = 60
    lock_ttl_s: int = 180
    steal_stale_lock: bool = False
    default_step_timeout_s: int = 900
    steps: List[StepCfg] = field(default_factory=list)


def _load_ops_cfg(control_state: Optional[Dict[str, Any]]) -> OpsCfg:
    """
    control_state.json example:

    {
      "ops_loop": {
        "enabled": true,
        "interval_s": 60,
        "lock_ttl_s": 180,
        "step_timeout_s": 900,
        "steal_stale_lock": false,
        "steps": [
          {"name": "wa_payload", "cmd": ["{PY}", "-m", "args.demo.demo_wa_v1_payload_from_latest_run"]},
          {"name": "sender_dryrun", "cmd": ["{PY}", "-m", "args.demo.demo_ibkr_sender_dryrun_from_latest_run"]},
          {"name": "sender_real", "cmd": ["{PY}", "-m", "args.ibkr.ibkr_sender_real_v1", "--run-id", "{RUN_ID}"], "timeout_s": 600}
        ]
      }
    }
    """
    if not isinstance(control_state, dict):
        return OpsCfg()

    ops = control_state.get("ops_loop")
    if not isinstance(ops, dict):
        return OpsCfg()

    enabled = _as_bool(ops.get("enabled"), False)
    once = _as_bool(ops.get("once"), False)

    interval_s = _as_int(ops.get("interval_s", 60), 60, min_v=1, max_v=86400)
    lock_ttl_s = _as_int(
        ops.get("lock_ttl_s", max(180, interval_s * 3)),
        max(180, interval_s * 3),
        min_v=10,
        max_v=86400,
    )
    steal_stale = _as_bool(ops.get("steal_stale_lock"), False)
    default_step_timeout_s = _as_int(ops.get("step_timeout_s", 900), 900, min_v=1, max_v=86400)

    steps_raw = ops.get("steps")
    steps: List[StepCfg] = []
    if isinstance(steps_raw, list):
        for i, sr in enumerate(steps_raw):
            if not isinstance(sr, dict):
                continue
            name = str(sr.get("name") or f"step_{i}").strip()
            cmd_raw = sr.get("cmd")
            if not isinstance(cmd_raw, list) or not cmd_raw:
                continue
            cmd = [str(x) for x in cmd_raw if str(x).strip()]
            if not cmd:
                continue
            timeout_s = _as_int(sr.get("timeout_s", default_step_timeout_s), default_step_timeout_s, min_v=1, max_v=86400)
            allow_fail = _as_bool(sr.get("allow_fail"), False)
            cwd = sr.get("cwd")
            cwd_s = str(cwd) if isinstance(cwd, (str, Path)) and str(cwd).strip() else None
            steps.append(StepCfg(name=name, cmd=cmd, timeout_s=timeout_s, allow_fail=allow_fail, cwd=cwd_s))

    return OpsCfg(
        enabled=enabled,
        once=once,
        interval_s=interval_s,
        lock_ttl_s=lock_ttl_s,
        steal_stale_lock=steal_stale,
        default_step_timeout_s=default_step_timeout_s,
        steps=steps,
    )


@dataclass
class LockHandle:
    """
    Lockfile is JSON with:
      owner_pid, owner_host, acquired_at_utc, heartbeat_at_utc, expires_at_utc
    """
    path: Path
    ttl_s: int
    steal_stale: bool
    acquired: bool = False
    owner_pid: int = field(default_factory=os.getpid)
    owner_host: str = field(default_factory=socket.gethostname)
    acquired_at_utc: Optional[str] = None

    def _lock_info(self, *, expires_at_utc: str) -> Dict[str, Any]:
        return {
            "schema": "lockfile_v1",
            "owner_pid": int(self.owner_pid),
            "owner_host": str(self.owner_host),
            "acquired_at_utc": self.acquired_at_utc or _utc_now_z(),
            "heartbeat_at_utc": _utc_now_z(),
            "expires_at_utc": expires_at_utc,
        }

    def _is_pid_alive(self, pid: int) -> Optional[bool]:
        """
        Returns:
          True  -> process seems alive
          False -> process seems dead
          None  -> can't determine (treat as alive for safety)
        """
        if pid <= 0:
            return None

        if os.name == "nt":
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            # If OpenProcess fails: either dead, or access denied. We can't reliably disambiguate here.
            # Treat as "unknown" => safe behaviour (no steal unless user truly wants unsafe mode).
            return None

        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return None

    def acquire(self) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        expires = now.timestamp() + float(self.ttl_s)
        expires_at_utc = datetime.fromtimestamp(expires, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        info = self._lock_info(expires_at_utc=expires_at_utc)

        for _ in range(3):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, (json.dumps(info, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
                self.acquired = True
                self.acquired_at_utc = info["acquired_at_utc"]
                return True, "ACQUIRED", info

            except FileExistsError:
                existing = _read_json_dict(self.path) or {}

                exp_dt = _parse_utc_ts(existing.get("expires_at_utc"))
                if exp_dt is not None:
                    stale = datetime.now(timezone.utc) > exp_dt
                else:
                    try:
                        age_s = time.time() - self.path.stat().st_mtime
                        stale = age_s > float(self.ttl_s)
                    except Exception:
                        stale = False

                if not stale:
                    return False, "LOCKED_ACTIVE", existing

                # Stale lock: by default we do NOT steal, even if it's stale.
                if not self.steal_stale:
                    return False, "LOCKED_STALE_NO_STEAL", existing

                ex_pid = int(existing.get("owner_pid") or 0) if str(existing.get("owner_pid") or "").isdigit() else 0
                alive = self._is_pid_alive(ex_pid) if ex_pid else None
                if alive is not False:
                    # Unknown / alive => refuse to steal (safe).
                    return False, "LOCKED_STALE_PID_UNKNOWN_OR_ALIVE", existing

                # pid confirmed dead => remove stale lock and retry acquire
                try:
                    self.path.unlink(missing_ok=True)
                except Exception:
                    return False, "LOCKED_STALE_UNLINK_FAILED", existing

                continue

            except Exception as e:
                return False, f"LOCK_ERROR:{type(e).__name__}", None

        return False, "LOCK_ACQUIRE_RETRY_EXHAUSTED", _read_json_dict(self.path)

    def heartbeat(self) -> None:
        if not self.acquired:
            return
        try:
            expires = datetime.now(timezone.utc).timestamp() + float(self.ttl_s)
            expires_at_utc = datetime.fromtimestamp(expires, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            info = _read_json_dict(self.path) or {}
            if int(info.get("owner_pid") or -1) != int(self.owner_pid):
                return
            info["heartbeat_at_utc"] = _utc_now_z()
            info["expires_at_utc"] = expires_at_utc
            _atomic_write_json(self.path, info if isinstance(info, dict) else self._lock_info(expires_at_utc=expires_at_utc))
        except Exception:
            return

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            info = _read_json_dict(self.path) or {}
            if int(info.get("owner_pid") or -1) == int(self.owner_pid):
                self.path.unlink(missing_ok=True)
        except Exception:
            return
        finally:
            self.acquired = False


def _stop_requested() -> bool:
    return STOP_FLAG_PATH.exists()


def _substitute_cmd(cmd: List[str], *, run_id: Optional[str], report_path: Optional[Path]) -> List[str]:
    out: List[str] = []
    for part in cmd:
        s = str(part)

        if s in {"{PY}", "{PYTHON}", "{PY_EXE}"}:
            out.append(sys.executable)
            continue

        if run_id is not None:
            s = s.replace("{RUN_ID}", run_id)
        if report_path is not None:
            s = s.replace("{LATEST_REPORT}", str(report_path))

        s = s.replace("{REPO_ROOT}", str(REPO_ROOT))
        s = s.replace("{DATA_DIR}", str(DATA_DIR))
        s = s.replace("{LOGS_DIR}", str(LOGS_DIR))

        out.append(s)

    return out


def _run_step(
    *,
    cycle_id: str,
    step_i: int,
    step: StepCfg,
    lock: LockHandle,
    run_id: Optional[str],
    report_path: Optional[Path],
) -> Dict[str, Any]:
    started = _utc_now_z()
    safe_step = _safe_name(step.name)
    log_path = LOGS_DIR / f"ops_{cycle_id}_{step_i:02d}_{safe_step}.log"

    # If config has a {RUN_ID} placeholder but no run_id exists => skip
    if any("{RUN_ID}" in str(x) for x in step.cmd) and not run_id:
        return {
            "name": step.name,
            "status": "SKIPPED_MISSING_RUN_ID",
            "cmd": _substitute_cmd(step.cmd, run_id=run_id, report_path=report_path),
            "started_at_utc": started,
            "ended_at_utc": _utc_now_z(),
            "duration_s": 0.0,
            "rc": None,
            "log_path": str(log_path),
            "allow_fail": bool(step.allow_fail),
        }

    cmd = _substitute_cmd(step.cmd, run_id=run_id, report_path=report_path)

    t0 = time.time()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as f:
            f.write(f"[{started}] START step={step.name}\n")
            f.write("CMD: " + " ".join(cmd) + "\n\n")
            f.flush()

            proc = subprocess.Popen(
                cmd,
                cwd=step.cwd or str(REPO_ROOT),
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            status = "OK"
            rc: Optional[int] = None
            deadline = t0 + float(step.timeout_s)

            while True:
                lock.heartbeat()

                if _stop_requested():
                    status = "STOPPED_BY_STOP_FLAG"
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break

                rc = proc.poll()
                if rc is not None:
                    break

                if time.time() >= deadline:
                    status = "TIMEOUT"
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    break

                time.sleep(0.5)

            # ensure process exits
            try:
                rc2 = proc.wait(timeout=5)
                if rc is None:
                    rc = rc2
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    rc3 = proc.wait(timeout=5)
                    if rc is None:
                        rc = rc3
                except Exception:
                    pass

            if status == "OK" and (rc is None or rc != 0):
                status = "ERROR"

            ended = _utc_now_z()
            dur = round(time.time() - t0, 3)
            f.write(f"\n[{ended}] END status={status} rc={rc} duration_s={dur}\n")
            f.flush()

            return {
                "name": step.name,
                "status": status,
                "cmd": cmd,
                "started_at_utc": started,
                "ended_at_utc": ended,
                "duration_s": dur,
                "rc": rc,
                "log_path": str(log_path),
                "allow_fail": bool(step.allow_fail),
            }

    except FileNotFoundError as e:
        ended = _utc_now_z()
        dur = round(time.time() - t0, 3)
        return {
            "name": step.name,
            "status": "ERROR",
            "cmd": cmd,
            "started_at_utc": started,
            "ended_at_utc": ended,
            "duration_s": dur,
            "rc": None,
            "log_path": str(log_path),
            "error": f"FILE_NOT_FOUND:{e}",
            "allow_fail": bool(step.allow_fail),
        }

    except Exception as e:
        ended = _utc_now_z()
        dur = round(time.time() - t0, 3)
        return {
            "name": step.name,
            "status": "ERROR",
            "cmd": cmd,
            "started_at_utc": started,
            "ended_at_utc": ended,
            "duration_s": dur,
            "rc": None,
            "log_path": str(log_path),
            "error": f"{type(e).__name__}:{e}",
            "allow_fail": bool(step.allow_fail),
        }


def _load_health(path: Path) -> Dict[str, Any]:
    obj = _read_json_dict(path)
    if isinstance(obj, dict) and obj.get("schema") == "ops_health_v1":
        return obj
    return {
        "schema": "ops_health_v1",
        "ts_utc": _utc_now_z(),
        "status": "INIT",
        "counters": {
            "total_cycles": 0,
            "total_failures": 0,
            "consecutive_failures": 0,
            "last_ok_at_utc": None,
            "last_error_at_utc": None,
        },
        "last_cycle": None,
        "last_error": None,
    }


def _update_health(
    *,
    status: str,
    cfg: OpsCfg,
    lock_status: Dict[str, Any],
    cycle: Optional[Dict[str, Any]],
    error: Optional[Dict[str, Any]] = None,
) -> None:
    h = _load_health(OPS_HEALTH_PATH)
    h["ts_utc"] = _utc_now_z()
    h["status"] = status

    h["config"] = {
        "enabled": cfg.enabled,
        "once": cfg.once,
        "interval_s": cfg.interval_s,
        "lock_ttl_s": cfg.lock_ttl_s,
        "steal_stale_lock": cfg.steal_stale_lock,
        "steps": [s.name for s in cfg.steps],
    }
    h["lock"] = lock_status

    if cycle is not None:
        h["last_cycle"] = cycle

        c = h.get("counters") if isinstance(h.get("counters"), dict) else {}
        c["total_cycles"] = int(c.get("total_cycles") or 0) + 1

        ok = bool(cycle.get("ok"))
        if ok:
            c["consecutive_failures"] = 0
            c["last_ok_at_utc"] = cycle.get("ended_at_utc")
        else:
            c["total_failures"] = int(c.get("total_failures") or 0) + 1
            c["consecutive_failures"] = int(c.get("consecutive_failures") or 0) + 1
            c["last_error_at_utc"] = cycle.get("ended_at_utc")

        h["counters"] = c

    if error is not None:
        h["last_error"] = error

    _atomic_write_json(OPS_HEALTH_PATH, h)


def _run_cycle(cfg: OpsCfg, lock: LockHandle) -> Tuple[Dict[str, Any], str]:
    t0 = time.time()
    cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    started = _utc_now_z()

    report = _latest_run_report_paper()
    run_id = _run_id_from_report_path(report) if report else None

    _append_jsonl(
        OPS_EVENTS_PATH,
        {
            "kind": "OPS_EVENT",
            "event": "CYCLE_START",
            "ts": started,
            "cycle_id": cycle_id,
            "run_id": run_id,
            "latest_report": str(report) if report else None,
        },
    )

    steps_out: List[Dict[str, Any]] = []
    overall_ok = True

    for i, step in enumerate(cfg.steps):
        if _stop_requested():
            overall_ok = False
            steps_out.append(
                {
                    "name": step.name,
                    "status": "SKIPPED_STOP_FLAG",
                    "started_at_utc": _utc_now_z(),
                    "ended_at_utc": _utc_now_z(),
                    "duration_s": 0.0,
                    "rc": None,
                    "log_path": None,
                    "allow_fail": bool(step.allow_fail),
                }
            )
            continue

        _append_jsonl(
            OPS_EVENTS_PATH,
            {"kind": "OPS_EVENT", "event": "STEP_START", "ts": _utc_now_z(), "cycle_id": cycle_id, "step": step.name},
        )

        r = _run_step(cycle_id=cycle_id, step_i=i, step=step, lock=lock, run_id=run_id, report_path=report)
        steps_out.append(r)

        _append_jsonl(
            OPS_EVENTS_PATH,
            {
                "kind": "OPS_EVENT",
                "event": "STEP_END",
                "ts": _utc_now_z(),
                "cycle_id": cycle_id,
                "step": step.name,
                "status": r.get("status"),
                "rc": r.get("rc"),
                "log_path": r.get("log_path"),
            },
        )

        st = str(r.get("status") or "")
        step_ok = (st == "OK") or st.startswith("SKIPPED") or (st == "STOPPED_BY_STOP_FLAG")
        if (not step_ok) and (not bool(r.get("allow_fail"))):
            overall_ok = False

    ended = _utc_now_z()
    duration_s = round(time.time() - t0, 3)

    _append_jsonl(OPS_EVENTS_PATH, {"kind": "OPS_EVENT", "event": "CYCLE_END", "ts": ended, "cycle_id": cycle_id, "ok": bool(overall_ok)})

    cycle = {
        "cycle_id": cycle_id,
        "started_at_utc": started,
        "ended_at_utc": ended,
        "duration_s": duration_s,
        "latest_report": str(report) if report else None,
        "run_id": run_id,
        "steps": steps_out,
        "ok": bool(overall_ok),
    }
    return cycle, ("OK" if overall_ok else "ERROR")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="auto_loop_v1",
        description="ARGS Stage7 Ops Loop (lock/TTL, stop.flag, ops_health.json)",
    )
    ap.add_argument("--once", action="store_true", help="Run exactly one cycle and exit.")
    ap.add_argument("--force", action="store_true", help="Run even if control_state.ops_loop.enabled is false/missing.")
    ap.add_argument("--interval-s", type=int, default=None, help="Override interval_s (seconds).")
    ap.add_argument("--lock-ttl-s", type=int, default=None, help="Override lock_ttl_s (seconds).")
    ap.add_argument("--steal-stale-lock", action="store_true", help="Allow stealing stale lockfile (dangerous).")
    args = ap.parse_args(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    control_state = _read_json_dict(DATA_DIR / "control_state.json")
    cfg = _load_ops_cfg(control_state)

    # CLI overrides
    if args.once:
        cfg.once = True
    if args.interval_s is not None:
        cfg.interval_s = max(1, int(args.interval_s))
    if args.lock_ttl_s is not None:
        cfg.lock_ttl_s = max(10, int(args.lock_ttl_s))
    if args.steal_stale_lock:
        cfg.steal_stale_lock = True

    if not cfg.enabled and not args.force:
        _update_health(
            status="DISABLED",
            cfg=cfg,
            lock_status={"path": str(LOCK_PATH), "acquired": False, "reason": "DISABLED"},
            cycle=None,
            error=None,
        )
        return 4

    if _stop_requested():
        _update_health(
            status="STOPPED",
            cfg=cfg,
            lock_status={"path": str(LOCK_PATH), "acquired": False, "reason": "STOP_FLAG"},
            cycle=None,
            error=None,
        )
        return 5

    lock = LockHandle(path=LOCK_PATH, ttl_s=cfg.lock_ttl_s, steal_stale=cfg.steal_stale_lock)

    ok, why, info = lock.acquire()
    if not ok:
        _append_jsonl(OPS_EVENTS_PATH, {"kind": "OPS_EVENT", "event": "LOCK_DENIED", "ts": _utc_now_z(), "reason": why, "lock": info})
        _update_health(
            status="LOCKED",
            cfg=cfg,
            lock_status={"path": str(lock.path), "acquired": False, "reason": why, "lock": info},
            cycle=None,
            error={"reason": why},
        )
        return 3

    _append_jsonl(OPS_EVENTS_PATH, {"kind": "OPS_EVENT", "event": "LOCK_ACQUIRED", "ts": _utc_now_z(), "lock_path": str(lock.path), "owner_pid": lock.owner_pid, "ttl_s": cfg.lock_ttl_s})

    try:
        while True:
            cycle, cyc_status = _run_cycle(cfg, lock)

            status = "OK" if cyc_status == "OK" else "ERROR"
            if _stop_requested():
                status = "STOPPED"

            lock_status = {"path": str(lock.path), "acquired": True, "owner_pid": lock.owner_pid}

            err = None
            if status == "ERROR":
                bad = None
                for s in cycle.get("steps") or []:
                    if str(s.get("status") or "") in {"ERROR", "TIMEOUT"} and not bool(s.get("allow_fail")):
                        bad = s
                        break
                err = {"reason": "CYCLE_ERROR", "step": bad} if bad else {"reason": "CYCLE_ERROR"}

            _update_health(status=status, cfg=cfg, lock_status=lock_status, cycle=cycle, error=err)

            if cfg.once:
                return 0 if status == "OK" else 6
            if status == "STOPPED":
                return 5

            # sleep until next cycle; keep heartbeating
            t_sleep0 = time.time()
            while True:
                lock.heartbeat()
                if _stop_requested():
                    return 5
                if (time.time() - t_sleep0) >= float(cfg.interval_s):
                    break
                time.sleep(0.5)

    finally:
        lock.release()
        _append_jsonl(OPS_EVENTS_PATH, {"kind": "OPS_EVENT", "event": "LOCK_RELEASED", "ts": _utc_now_z(), "lock_path": str(lock.path), "owner_pid": lock.owner_pid})


if __name__ == "__main__":
    raise SystemExit(main())
