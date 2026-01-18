import argparse
import sys

__VERSION__ = "0.0.0"

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ecosystem_console_v0",
        description="Ecosystem Console v0 (placeholder, stdlib-only)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="Health ping (stdout OK).")
    sub.add_parser("version", help="Print version.")
    sub.add_parser("selftest", help="Run minimal self-test.")

    return p

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = build_parser()
    args = p.parse_args(argv)

    if args.cmd == "ping":
        print("OK")
        return 0
    if args.cmd == "version":
        print(__VERSION__)
        return 0
    if args.cmd == "selftest":
        print("SELFTEST_OK")
        return 0

    return 2

if __name__ == "__main__":
    raise SystemExit(main())
