import tempfile
import unittest
from pathlib import Path

from args_core.control_plane_v0 import ControlPlane
from args_core.demo_stage5_v1 import FakeBroker
from args_core.execution_v1 import Engine, OrderIntent, OrderSpec


class TestStage5RestartIdempotency(unittest.TestCase):
    def test_restart_without_state_no_duplicate_place(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "runs").mkdir(parents=True, exist_ok=True)

            cp = ControlPlane(
                global_mode="ONLY_EXITS", run_root="runs", cool_down_seconds=1
            )
            cp_path = repo / "control_plane.json"
            cp.save(cp_path)

            broker = FakeBroker(scenario="fill", positions={"AAPL": 2.0})

            run_id = "rid"
            eng1 = Engine(
                repo_root=repo, run_id=run_id, control_plane_path=cp_path, broker=broker
            )
            eng1.submit_intent(
                OrderIntent(
                    intent_id="i1", order=OrderSpec(symbol="AAPL", side="SELL", qty=2.0)
                )
            )

            eng1.step()  # should place once
            self.assertEqual(eng1.state.counters["place_calls"], 1)

            # delete state.json to simulate crash
            state_path = repo / "runs" / run_id / "state.json"
            self.assertTrue(state_path.exists())
            state_path.unlink()

            # restart with same broker snapshot (open order present)
            eng2 = Engine(
                repo_root=repo, run_id=run_id, control_plane_path=cp_path, broker=broker
            )
            before = eng2.state.counters["place_calls"]
            eng2.step()
            after = eng2.state.counters["place_calls"]

            self.assertEqual(before, after, "restart must not place duplicate order")


if __name__ == "__main__":
    unittest.main()
