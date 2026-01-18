from __future__ import annotations

import argparse
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .common_v0 import atomic_write_json, utc_now_iso
from .execution_v1 import Engine, OrderSpec
from .ibkr_file_adapter_v0 import IbkrFileAdapterV0


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


class LockFile:
    def __init__(self, path: Path, stale_after_s: int = 300) -> None:
        self.path = path
        self.stale_after_s = int(stale_after_s)
        self.acquired = False

    def acquire(self) -> bool:
        _ensure_dir(self.path.parent)
        if self.path.exists():
            age = time.time() - self.path.stat().st_mtime
            if age > self.stale_after_s:
                try:
                    self.path.unlink()
                except Exception:
                    pass

        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {"pid": os.getpid(), "ts_utc": utc_now_iso()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            self.acquired = True
            return True
        except FileExistsError:
            return False

    def heartbeat(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.write_text(
                json.dumps(
                    {"pid": os.getpid(), "heartbeat_utc": utc_now_iso()},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except Exception:
            pass
        self.acquired = False


def setup_logger(log_path: Path) -> logging.Logger:
    _ensure_dir(log_path.parent)
    logger = logging.getLogger("soak_stage7_v1")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    fh = RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--interval", type=float, default=1.0)

    ap.add_argument("--positions", default=r"args\data\ibkr_positions_live.json")
    ap.add_argument("--orders", default=r"args\data\ibkr_open_orders_live.json")
    ap.add_argument("--events", default=r"args\data\ibkr_events_live.jsonl")
    ap.add_argument("--cursor", default=r"args\data\ibkr_events.cursor.json")
    ap.add_argument("--reset-cursor", action="store_true")

    ap.add_argument("--control-plane", default=r"args\data\control_plane.json")

    # adopt target
    ap.add_argument("--adopt-order-id", type=int, default=1018)
    ap.add_argument("--adopt-symbol", default="AAPL")
    ap.add_argument("--adopt-side", default="SELL")
    ap.add_argument("--adopt-qty", type=float, default=2.0)

    # ops outputs
    ap.add_argument("--health-out", default=r"args\data\ops_health.json")
    ap.add_argument("--log-out", default=r"args\logs\soak_stage7.log")
    ap.add_argument("--lock-out", default=r"args\logs\soak_stage7.lock")
    ap.add_argument("--lock-stale-s", type=int, default=300)

    # ---- Stage7 standard additions ----
    # lock override for evidence/chaos runs (isolates from live loop)
    ap.add_argument(
        "--lock-path",
        default="",
        help="Override lock file path (useful for tests/evidence).",
    )

    # Backward-compat knob (ignored by standard). Kept to not break existing callers.
    ap.add_argument(
        "--locked-exit-code",
        type=int,
        default=0,
        help="(ignored) Stage7 standard always exits 0 on LOCKED.",
    )

    args = ap.parse_args()

    repo = Path(args.repo)

    stop_flag = repo / "stop.flag"
    safe_flag = repo / "safe_mode.flag"

    logger = setup_logger(repo / args.log_out)

    lock_path = (
        Path(args.lock_path) if str(args.lock_path).strip() else (repo / args.lock_out)
    )
    lock = LockFile(lock_path, stale_after_s=args.lock_stale_s)

    # ---- Stage7 standard: LOCKED is OK (exit 0) ----
    if not lock.acquire():
        print(
            json.dumps(
                {"ok": True, "reason": "LOCKED", "lock": str(lock_path)},
                ensure_ascii=False,
            )
        )
        return 0

    started = time.time()
    end = started + float(args.seconds)

    health_path = repo / args.health_out
    _ensure_dir(health_path.parent)

    def _final_health(
        mode: str, loops: int, run_id: str, eng: Engine, last_err: Optional[str]
    ) -> None:
        atomic_write_json(
            health_path,
            {
                "schema": "ops_health_v1",
                "ts_utc": utc_now_iso(),
                "ok": True
                if mode in {"DONE", "STOPPED", "INTERRUPTED"} and not last_err
                else False,
                "mode": mode,
                "loops": loops,
                "run_id": run_id,
                "uptime_s": round(time.time() - started, 3),
                "stop_flag": stop_flag.exists(),
                "safe_mode": safe_flag.exists(),
                "events_seen": eng.state.counters.get("events_seen"),
                "reconcile_ratio": eng.state.reconcile_last_ratio,
                "last_error": last_err,
            },
        )

    try:
        # Control plane path
        cp_path = repo / args.control_plane
        if not cp_path.exists():
            cp_path = repo / "control_plane.json"

        # Cursor reset if requested
        cursor_path = repo / args.cursor
        if args.reset_cursor:
            try:
                cursor_path.unlink()
            except FileNotFoundError:
                pass

        # Adapter (READ-ONLY)
        events_path = repo / args.events
        adapter = IbkrFileAdapterV0(
            repo_root=repo,
            positions_path=repo / args.positions,
            open_orders_path=repo / args.orders,
            events_jsonl_path=events_path if events_path.exists() else None,
            events_cursor_path=cursor_path,
        )
        adapter.connect()

        run_id = f"soak_stage7_{int(time.time())}"
        eng = Engine(
            repo_root=repo, run_id=run_id, control_plane_path=cp_path, broker=adapter
        )

        # Adopt a known live order (safe)
        intent_id = f"adopt_{args.adopt_order_id}"
        eng.adopt_order(
            intent_id=intent_id,
            order_id=int(args.adopt_order_id),
            order=OrderSpec(
                symbol=args.adopt_symbol,
                side=args.adopt_side,
                qty=float(args.adopt_qty),
            ),
            client_order_id=f"oid_{args.adopt_order_id}",
            remaining_qty=float(args.adopt_qty),
        )

        last_err: Optional[str] = None
        loops = 0
        backoff = 0.0

        try:
            while time.time() < end:
                loops += 1
                lock.heartbeat()

                if stop_flag.exists():
                    logger.warning("stop.flag detected -> exiting (fail-closed)")
                    _final_health("STOPPED", loops, run_id, eng, last_err)
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "reason": "STOP_FLAG",
                                "loops": loops,
                                "health": str(health_path),
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 0

                if safe_flag.exists():
                    logger.warning(
                        "safe_mode.flag detected -> SAFE_MODE (read-only loop)"
                    )

                try:
                    eng.step()
                    last_err = eng.state.last_error
                    backoff = 0.0
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    backoff = min(30.0, backoff * 2.0 + 1.0)

                t = eng.state.tickets.get(intent_id)
                health = {
                    "schema": "ops_health_v1",
                    "ts_utc": utc_now_iso(),
                    "ok": True if not last_err else False,
                    "mode": "RUNNING",
                    "run_id": run_id,
                    "loops": loops,
                    "uptime_s": round(time.time() - started, 3),
                    "stop_flag": stop_flag.exists(),
                    "safe_mode": safe_flag.exists(),
                    "broker_connected": adapter.is_connected(),
                    "events_seen": eng.state.counters.get("events_seen"),
                    "reconcile_ratio": eng.state.reconcile_last_ratio,
                    "last_error": last_err,
                    "ticket_state": t.state.value if t else None,
                    "ticket_terminal": (t.terminal.value if t and t.terminal else None),
                    "ticket_remaining": (t.remaining_qty if t else None),
                }
                atomic_write_json(health_path, health)

                if loops % 10 == 0:
                    logger.info(
                        f"loops={loops} events_seen={health['events_seen']} reconcile={health['reconcile_ratio']} "
                        f"state={health['ticket_state']} terminal={health['ticket_terminal']} err={last_err}"
                    )

                if backoff > 0:
                    time.sleep(backoff)
                else:
                    time.sleep(float(args.interval))

        except KeyboardInterrupt:
            logger.warning("KeyboardInterrupt -> graceful stop")
            _final_health("INTERRUPTED", loops, run_id, eng, last_err)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "reason": "INTERRUPTED",
                        "loops": loops,
                        "health": str(health_path),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        _final_health("DONE", loops, run_id, eng, last_err)
        print(
            json.dumps(
                {
                    "ok": True,
                    "reason": "DONE",
                    "loops": loops,
                    "health": str(health_path),
                },
                ensure_ascii=False,
            )
        )
        return 0

    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
