import tempfile
import unittest
from pathlib import Path

from args_core.control_plane_v0 import ControlPlane
from args_core.demo_stage5_v1 import FakeBroker
from args_core.execution_v1 import Engine, OrderIntent, OrderSpec, TerminalState


class TestTerminalInvariants(unittest.TestCase):
    def test_terminal_state_is_one_of_expected(self):
        scenarios = ["fill", "reject", "partial_then_fill", "partial_then_cancel"]
        for sc in scenarios:
            with tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                (repo / "runs").mkdir(parents=True, exist_ok=True)
                cp = ControlPlane(global_mode="ONLY_EXITS", run_root="runs", cool_down_seconds=1)
                cp_path = repo / "control_plane.json"
                cp.save(cp_path)

                broker = FakeBroker(scenario=sc, positions={"AAPL": 2.0})
                eng = Engine(repo_root=repo, run_id=f"r_{sc}", control_plane_path=cp_path, broker=broker)
                eng.submit_intent(OrderIntent(intent_id="i1", order=OrderSpec(symbol="AAPL", side="SELL", qty=2.0)))

                for _ in range(60):
                    eng.step()
                    t = eng.state.tickets["i1"]
                    if sc == "partial_then_cancel" and t.state.value == "PARTIAL":
                        eng.request_cancel("i1")
                    if t.is_terminal():
                        break

                t = eng.state.tickets["i1"]
                self.assertTrue(t.is_terminal())
                self.assertIn(t.terminal, {TerminalState.DONE, TerminalState.FAILED, TerminalState.CANCELLED, TerminalState.FILLED})
