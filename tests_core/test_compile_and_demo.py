import tempfile
import unittest
from pathlib import Path

from args_core.control_plane_v0 import ControlPlane
from args_core.demo_stage5_v1 import FakeBroker
from args_core.execution_v1 import Engine, OrderIntent, OrderSpec


class TestStage5Basic(unittest.TestCase):
    def test_fill_goes_done_and_flat(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "runs").mkdir(parents=True, exist_ok=True)

            cp = ControlPlane(
                global_mode="ONLY_EXITS", run_root="runs", cool_down_seconds=1
            )
            cp_path = repo / "control_plane.json"
            cp.save(cp_path)

            broker = FakeBroker(scenario="fill", positions={"AAPL": 2.0})
            eng = Engine(
                repo_root=repo, run_id="r1", control_plane_path=cp_path, broker=broker
            )

            eng.submit_intent(
                OrderIntent(
                    intent_id="i1", order=OrderSpec(symbol="AAPL", side="SELL", qty=2.0)
                )
            )
            for _ in range(10):
                eng.step()
                t = eng.state.tickets["i1"]
                if t.is_terminal():
                    break

            t = eng.state.tickets["i1"]
            self.assertTrue(t.is_terminal())
            self.assertEqual(t.terminal.value, "DONE")
            self.assertAlmostEqual(
                float(broker.snapshot()["positions"]["AAPL"]), 0.0, places=9
            )

    def test_stop_flag_blocks_place(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "runs").mkdir(parents=True, exist_ok=True)
            (repo / "stop.flag").write_text("1", encoding="utf-8")

            cp = ControlPlane(
                global_mode="ONLY_EXITS",
                run_root="runs",
                kill_switch_file="stop.flag",
                cool_down_seconds=1,
            )
            cp_path = repo / "control_plane.json"
            cp.save(cp_path)

            broker = FakeBroker(scenario="fill", positions={"AAPL": 2.0})
            eng = Engine(
                repo_root=repo, run_id="r2", control_plane_path=cp_path, broker=broker
            )

            eng.submit_intent(
                OrderIntent(
                    intent_id="i1", order=OrderSpec(symbol="AAPL", side="SELL", qty=2.0)
                )
            )
            before = eng.state.counters["place_calls"]
            eng.step()
            after = eng.state.counters["place_calls"]
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
