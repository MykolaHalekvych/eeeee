from __future__ import annotations

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cli import main  # noqa: E402


class TestSmoke(unittest.TestCase):
    def test_ping(self) -> None:
        self.assertEqual(main(["ping"]), 0)

    def test_version(self) -> None:
        self.assertEqual(main(["version"]), 0)

    def test_hash(self) -> None:
        self.assertEqual(main(["hash"]), 0)

    def test_selftest(self) -> None:
        self.assertEqual(main(["selftest"]), 0)


if __name__ == "__main__":
    unittest.main()
