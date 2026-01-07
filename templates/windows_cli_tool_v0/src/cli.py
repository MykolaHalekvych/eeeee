from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app",
        description="windows_cli_tool_v0: minimal CLI scaffold (EXE Pack v0)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_hello = sub.add_parser("hello", help="print a hello message")
    p_hello.add_argument("--name", default="world")

    sub.add_parser("version", help="print version")

    p_hash = sub.add_parser("hash", help="print sha256 (JSON) for a file")
    p_hash.add_argument("path")

    return p
