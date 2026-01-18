from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> tuple[int, dict]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout or "").strip().splitlines()
    if not out:
        return p.returncode, {"ok": False, "error": "no_stdout"}
    try:
        obj = json.loads(out[-1])
    except Exception:
        return p.returncode, {
            "ok": False,
            "error": "stdout_not_json",
            "stdout_tail": out[-1],
        }
    return p.returncode, obj


def main() -> int:
    repo = Path(r"C:\Users\mukol\ARGS-Core-v1")
    py = [
        "py",
        "-3.11",
        "-m",
        "args.stage5.terminal_scenarios.terminal_scenarios_v1",
        "--repo",
        str(repo),
    ]
    contract = [
        "--contract-json",
        "args/data/ibkr_mhg_contract_v1.json",
        "--symbol",
        "MHG",
    ]

    cases = [
        ("scenario_cancelled_v1", []),
        (
            "scenario_rejected_v1",
            ["--confirm-paper"],
        ),  # still disabled under DRYRUN; confirm-paper shouldn't bypass
        ("scenario_fill_v1", ["--confirm-fill", "YES"]),
    ]

    results = []
    ok = True
    for scen, extra in cases:
        cmd = py + ["--scenario", scen] + contract + extra
        rc, obj = run(cmd)
        results.append({"scenario": scen, "rc": rc, "json": obj})
        if rc != 2:
            ok = False
        if not isinstance(obj, dict) or obj.get("exit_code") != 2:
            ok = False

    out = {
        "schema": "demo_terminal_scenarios_dryrun_smoke_v1",
        "ok": ok,
        "results": results,
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
