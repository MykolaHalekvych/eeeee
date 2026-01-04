# args/demo/demo_stage7_as_v1.py
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

PY_DEFAULT = r"C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--py", default=PY_DEFAULT)
    ap.add_argument("--universe", default="MHG")
    ap.add_argument("--cooldown-sec", type=int, default=900)
    args = ap.parse_args()

    repo = Path(args.repo)
    cmd = [
        args.py,
        "-m",
        "args.stage7.as_v1",
        "--repo",
        str(repo),
        "--universe",
        args.universe,
        "--cooldown-sec",
        str(args.cooldown_sec),
    ]
    print("RUN:", " ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
