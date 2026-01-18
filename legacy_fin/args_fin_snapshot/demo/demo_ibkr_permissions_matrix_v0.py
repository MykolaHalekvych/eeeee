# args/demo/demo_ibkr_permissions_matrix_v0.py
from args.control.ibkr_effective_permissions_v0 import compute_effective_permissions

CASES = [
    ("DRY_RUN", "ALLOW_NEW_ENTRIES"),
    ("DRY_RUN", "ONLY_EXITS"),
    ("DRY_RUN", "NO_TRADE"),
    ("EXIT_ONLY", "ALLOW_NEW_ENTRIES"),
    ("EXIT_ONLY", "ONLY_EXITS"),
    ("EXIT_ONLY", "NO_TRADE"),
    ("FULL", "ALLOW_NEW_ENTRIES"),
    ("FULL", "ONLY_EXITS"),
    ("FULL", "NO_TRADE"),
]


def main() -> None:
    for e, r in CASES:
        p = compute_effective_permissions(e, r)
        print(
            f"exec={e:9} risk={r:16} | "
            f"force_sim={p.force_simulate} "
            f"cancel={p.allow_cancel_all} entry={p.allow_entry_orders} exit={p.allow_exit_orders}"
        )

    # Hard assertions for Stage 8 readiness
    p = compute_effective_permissions("DRY_RUN", "ALLOW_NEW_ENTRIES")
    assert p.force_simulate is True
    assert p.allow_entry_orders is True

    p = compute_effective_permissions("EXIT_ONLY", "ALLOW_NEW_ENTRIES")
    assert p.allow_entry_orders is False

    p = compute_effective_permissions("FULL", "NO_TRADE")
    assert p.allow_entry_orders is False
    assert p.allow_exit_orders is False
    assert p.allow_cancel_all is True

    print("OK: permission matrix assertions passed")


if __name__ == "__main__":
    main()
