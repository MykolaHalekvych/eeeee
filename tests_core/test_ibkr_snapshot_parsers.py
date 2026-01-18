import json
import tempfile
import unittest
from pathlib import Path

from args_core.ibkr_file_adapter_v0 import (
    parse_positions_snapshot,
    parse_open_orders_snapshot,
)


class TestIbkrSnapshotParsers(unittest.TestCase):
    def test_positions_rows(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "pos.json"
            p.write_text(
                json.dumps(
                    {"schema": "x", "rows": [{"symbol": "AAPL", "position": 2.0}]}
                ),
                encoding="utf-8",
            )
            pos = parse_positions_snapshot(p)
            self.assertEqual(pos["AAPL"], 2.0)

    def test_orders_rows(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "oo.json"
            p.write_text(
                json.dumps(
                    {
                        "schema": "x",
                        "rows": [
                            {
                                "orderId": 1018,
                                "symbol": "AAPL",
                                "status": "PreSubmitted",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            oo = parse_open_orders_snapshot(p)
            self.assertIn("1018", oo)


if __name__ == "__main__":
    unittest.main()
