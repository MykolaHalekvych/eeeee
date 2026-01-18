import sys

def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print("Ecosystem Console v0 (placeholder). Use CLI: ping/version/selftest.")
        return 0
    print("Ecosystem Console v0 placeholder. Use CLI.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
