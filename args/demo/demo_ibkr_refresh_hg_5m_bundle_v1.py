from __future__ import annotations

import subprocess
import sys


def run_mod(mod: str) -> int:
    print("\n" + "=" * 72)
    print(f"RUN: {mod}")
    print("=" * 72)
    p = subprocess.run([sys.executable, "-m", mod])
    return int(p.returncode)


def main() -> int:
    steps = [
        "args.demo.demo_ibkr_contract_resolve_hg",   # writes args/data/ibkr_hg_contract_v1.json
        "args.demo.demo_ibkr_fetch_hg_5m",           # writes args/data/hg_5m_bars_ibkr.csv (existing Stage19B demo)
        "args.demo.demo_attach_contract_meta_hg",    # writes args/data/hg_5m_bars_ibkr.meta.json
    ]

    for mod in steps:
        code = run_mod(mod)
        if code != 0:
            print(f"\nFAILED: {mod} exit_code={code}")
            return code

    print("\nDONE: HG bundle refreshed (contract + bars + meta).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
