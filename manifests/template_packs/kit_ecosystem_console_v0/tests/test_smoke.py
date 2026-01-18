from __future__ import annotations

import unittest

from src.cli import main


class TestSmoke(unittest.TestCase):
    def test_ping(self) -> None:
        self.assertEqual(main(["ping"]), 0)

    def test_version(self) -> None:
        self.assertEqual(main(["version"]), 0)

    def test_selftest(self) -> None:
        self.assertEqual(main(["selftest"]), 0)


if __name__ == "__main__":
    unittest.main()
