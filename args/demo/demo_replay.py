"""
Demo runner for replay harness.

Runs offline replay:
- policy: args/data/invariants_hg_v0.yaml
- events: args/data/events.jsonl

Prints summary and up to N mismatch examples.
"""

from pathlib import Path

from args.audit.replay_harness import replay_events


def _repo_root() -> Path:
    # args/demo/demo_replay.py -> repo root
    return Path(__file__).resolve().parents[2]


def main() -> int:
    root = _repo_root()
    policy_path = root / "args" / "data" / "invariants_hg_v0.yaml"
    events_path = root / "args" / "data" / "events.jsonl"

    report = replay_events(str(events_path), str(policy_path), max_mismatches=5)

    print(
        f"REPLAY total={report['total']} matches={report['matches']} mismatches={report['mismatches']}"
    )

    if report["mismatches"] > 0:
        print("Mismatch examples (first up to 5):")
        for ex in report["mismatch_examples"]:
            print(
                f"- line={ex['line']} event_id={ex.get('event_id')} expected={ex['expected']} actual={ex['actual']}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
