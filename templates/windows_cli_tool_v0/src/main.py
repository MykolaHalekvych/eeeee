from __future__ import annotations

import json
import sys
from pathlib import Path

from cli import build_parser

__version__ = "0.1.0"


def cmd_hello(name: str) -> int:
    print(f"hello, {name}")
    return 0


def cmd_version() -> int:
    print(__version__)
    return 0


def cmd_hash(path: str) -> int:
    import hashlib

    p = Path(path)
    if not p.exists() or not p.is_file():
        print(f"ERROR: file not found: {p}", file=sys.stderr)
        return 2

    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    out = {
        "schema": "sha256_v0",
        "path": str(p),
        "sha256": h.hexdigest(),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "hello":
        return cmd_hello(args.name)
    if args.command == "version":
        return cmd_version()
    if args.command == "hash":
        return cmd_hash(args.path)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
