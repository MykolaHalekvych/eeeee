import unittest
from typing import Any, Dict

from args_core.ibkr_event_normalizer_v0 import normalize_ibkr_record
from args_core.execution_v1 import BrokerEvent, EventType, _fill_dedup_key_v0


class TestStage5FillDedupV0(unittest.TestCase):
    def test_exec_details_reason_is_execid(self):
        rec: Dict[str, Any] = {
            "type": "EXEC_DETAILS",
            "order_id": 1018,
            "client_order_id": "oid_1018",
            "symbol": "AAPL",
            "shares": 1,
            "execution": {"execId": "X123"},
            "ts_utc": "2026-01-02T08:00:00Z",
        }
        ev = normalize_ibkr_record(rec)
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.event_type, EventType.FILL)
        self.assertEqual(ev.reason, "execId:X123")

    def test_fill_dedup_key_stable(self):
        ev1 = BrokerEvent(
            EventType.FILL,
            1018,
            "oid_1018",
            "AAPL",
            1.0,
            0.0,
            "execId:X1",
            "2026-01-02T08:00:00Z",
        )
        ev2 = BrokerEvent(
            EventType.FILL,
            1018,
            "oid_1018",
            "AAPL",
            1.00,
            0.0,
            "execId:X1",
            "2026-01-02T08:00:00Z",
        )
        self.assertEqual(_fill_dedup_key_v0(ev1), _fill_dedup_key_v0(ev2))

    def test_fill_dedup_key_diff_execid(self):
        ev1 = BrokerEvent(
            EventType.FILL,
            1018,
            "oid_1018",
            "AAPL",
            1.0,
            0.0,
            "execId:X1",
            "2026-01-02T08:00:00Z",
        )
        ev2 = BrokerEvent(
            EventType.FILL,
            1018,
            "oid_1018",
            "AAPL",
            1.0,
            0.0,
            "execId:X2",
            "2026-01-02T08:00:00Z",
        )
        self.assertNotEqual(_fill_dedup_key_v0(ev1), _fill_dedup_key_v0(ev2))


if __name__ == "__main__":
    unittest.main()
