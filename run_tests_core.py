import unittest


def main() -> int:
    suite = unittest.defaultTestLoader.discover("tests_core", pattern="test_*.py")
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
