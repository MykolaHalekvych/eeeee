from __future__ import annotations

import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.product_manifest_v1 import validate_manifest  # noqa: E402


class TestManifestV1(unittest.TestCase):
    def test_sample_manifest_valid(self) -> None:
        p = ROOT / "src" / "sample_product_manifest_v1.json"
        obj = json.loads(p.read_text(encoding="utf-8-sig"))
        ok, errs = validate_manifest(obj)
        self.assertTrue(ok, msg=str(errs))


if __name__ == "__main__":
    unittest.main()
