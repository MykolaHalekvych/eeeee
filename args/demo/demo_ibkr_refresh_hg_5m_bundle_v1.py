
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "args" / "data"


def _run(mod: str) -> None:
    print("\n" + "=" * 72)
    print(f"RUN: {mod}")
    print("=" * 72)
    r = subprocess.run([sys.executable, "-m", mod], cwd=str(REPO_ROOT))
    if r.returncode != 0:
        raise SystemExit(r.returncode)


def _alias(src: Path, dst: Path) -> None:
    if not src.exists():
        raise RuntimeError(f"ALIAS source missing: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def main() -> int:
    # 1) Resolve MHG contract (unattended-safe)
    _run("args.demo.demo_ibkr_contract_resolve_mhg")

    # 2) Fetch MHG 5m bars -> mhg_5m_bars_ibkr.csv
    _run("args.demo.demo_ibkr_fetch_mhg_5m")

    # 3) Attach MHG contract meta -> mhg_5m_bars_ibkr.meta.json
    _run("args.demo.demo_attach_contract_meta_mhg")

    # 4) Back-compat aliases for existing pipeline (paper_loop expects hg_* filenames)
    _alias(DATA_DIR / "mhg_5m_bars_ibkr.csv", DATA_DIR / "hg_5m_bars_ibkr.csv")
    _alias(DATA_DIR / "mhg_5m_bars_ibkr.meta.json", DATA_DIR / "hg_5m_bars_ibkr.meta.json")

    # optional: contract aliases (some legacy code may still read ibkr_hg_contract_v1.json)
    _alias(DATA_DIR / "ibkr_mhg_contract_v1.json", DATA_DIR / "ibkr_hg_contract_v1.json")
    _alias(DATA_DIR / "ibkr_mhg_contract_v1.meta.json", DATA_DIR / "ibkr_hg_contract_v1.meta.json")

    print("\nDONE: MHG bundle refreshed (contract + bars + meta) + HG aliases updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
