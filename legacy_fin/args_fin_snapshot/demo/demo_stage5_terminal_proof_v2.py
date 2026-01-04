# args/demo/demo_stage5_terminal_proof_v2.py
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

PY_DEFAULT = r"C:\Users\mukol\AppData\Local\Programs\Python\Python311\python.exe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--py", default=PY_DEFAULT)
    ap.add_argument("--input-jsonl", default="")
    ap.add_argument("--cursor", default="")
    ap.add_argument("--out-root", default="")
    args = ap.parse_args()

    repo = Path(args.repo)
    py = args.py

    mod = "args.stage5.terminal.terminal_proof_pack_v2"
    cmd = [py, "-m", mod, "--repo", str(repo)]

    if args.input_jsonl:
        cmd += ["--input-jsonl", args.input_jsonl]

        # Fixture-safe defaults (do NOT touch live cursor/proofs)
        if not args.cursor:
            base = Path(args.input_jsonl).stem
            tmp = repo / "args" / "data" / "_tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            args.cursor = str(tmp / f"stage5_terminal_proof_v2_fixture_{base}.cursor.json")

        if not args.out_root:
            out = repo / "args" / "_tmp" / "terminal_proofs_fixtures"
            out.mkdir(parents=True, exist_ok=True)
            args.out_root = str(out)

    if args.cursor:
        cmd += ["--cursor", args.cursor]
    if args.out_root:
        cmd += ["--out-root", args.out_root]

    print("RUN:", " ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
