import tempfile
import unittest
from pathlib import Path

from args_core.control_plane_v0 import ControlPlane
from args_core.demo_stage5_v1 import FakeBroker
from args_core.execution_v1 import Engine, OrderIntent, OrderSpec


class TestStage5MorePaths(unittest.TestCase):
    def _mk(self, scenario: str):
        td = tempfile.TemporaryDirectory()
        repo = Path(td.name)
        (repo / "runs").mkdir(parents=True, exist_ok=True)
        cp = ControlPlane(global_mode="ONLY_EXITS", run_root="runs", cool_down_seconds=1)
        cp_path = repo / "control_plane.json"
        cp.save(cp_path)
        broker = FakeBroker(scenario=scenario, positions={"AAPL": 2.0})
        eng = Engine(repo_root=repo, run_id=f"r_{scenario}", control_plane_path=cp_path, broker=broker)
        return td, repo, eng, broker, cp_path

    def test_reject_goes_failed(self):
        td, repo, eng, broker, _ = self._mk("reject")
        try:
            eng.submit_intent(OrderIntent(intent_id="i1", order=OrderSpec(symbol="AAPL", side="SELL", qty=2.0)))
            for _ in range(10):
                eng.step()
                t = eng.state.tickets["i1"]
                if t.is_terminal():
                    break
            t = eng.state.tickets["i1"]
            self.assertTrue(t.is_terminal())
            self.assertEqual(t.terminal.value, "FAILED")
        finally:
            td.cleanup()

    def test_partial_then_cancel_goes_cancelled(self):
        td, repo, eng, broker, _ = self._mk("partial_then_cancel")
        try:
            eng.submit_intent(OrderIntent(intent_id="i1", order=OrderSpec(symbol="AAPL", side="SELL", qty=2.0)))
            for _ in range(30):
                eng.step()
                t = eng.state.tickets["i1"]
                if t.state.value == "PARTIAL":
                    eng.request_cancel("i1")
                if t.is_terminal():
                    break
            t = eng.state.tickets["i1"]
            self.assertTrue(t.is_terminal())
            self.assertIn(t.terminal.value, ["CANCELLED", "FAILED"])
        finally:
            td.cleanup()

    def test_partial_then_fill_goes_done_and_flat(self):
        td, repo, eng, broker, _ = self._mk("partial_then_fill")
        try:
            eng.submit_intent(OrderIntent(intent_id="i1", order=OrderSpec(symbol="AAPL", side="SELL", qty=2.0)))
            for _ in range(30):
                eng.step()
                t = eng.state.tickets["i1"]
                if t.is_terminal():
                    break
            t = eng.state.tickets["i1"]
            self.assertTrue(t.is_terminal())
            self.assertEqual(t.terminal.value, "DONE")
            self.assertAlmostEqual(float(broker.snapshot()["positions"]["AAPL"]), 0.0, places=9)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
