import unittest

from args_core.ibkr_event_normalizer_v0 import normalize_ibkr_record
from args_core.execution_v1 import EventType


class TestIbkrEventNormalizer(unittest.TestCase):
    def test_ack_from_status(self):
        rec = {
            "type": "ORDER_STATUS",
            "orderId": 1018,
            "status": "PreSubmitted",
            "symbol": "AAPL",
        }
        ev = normalize_ibkr_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, EventType.ACK)
        self.assertEqual(ev.order_id, 1018)

    def test_reject_from_error(self):
        rec = {"type": "ERROR", "orderId": 1018, "code": 201, "msg": "Order rejected"}
        ev = normalize_ibkr_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, EventType.REJECT)
        self.assertEqual(ev.order_id, 1018)

    def test_fill_partial(self):
        rec = {
            "event_type": "FILL",
            "order_id": 1018,
            "filled": 1,
            "remaining": 1,
            "symbol": "AAPL",
        }
        ev = normalize_ibkr_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, EventType.FILL)
        self.assertEqual(ev.remaining_qty, 1)

    def test_fill_terminal(self):
        rec = {
            "status": "Filled",
            "orderId": 1018,
            "filled": 2,
            "remaining": 0,
            "symbol": "AAPL",
        }
        ev = normalize_ibkr_record(rec)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, EventType.FILL)
        self.assertEqual(ev.remaining_qty, 0)


if __name__ == "__main__":
    unittest.main()
