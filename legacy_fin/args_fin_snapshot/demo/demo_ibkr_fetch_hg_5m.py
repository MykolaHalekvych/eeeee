from __future__ import annotations

from pathlib import Path

from args.ibkr.ibkr_fetch_bars_v0 import FetchCfg, fetch_historical_bars


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    out_csv = repo_root / "args" / "data" / "hg_5m_bars_ibkr.csv"

    # NOTE: For futures, IBKR often needs specific contract month.
    # We try CONTFUT first (continuous). If your TWS returns "no security definition",
    # we will add contract-details resolution in the fetcher next.
    cfg = FetchCfg(
        symbol="HG",
        exchange="COMEX",
        currency="USD",
        sec_type="CONTFUT",  # try continuous future first
        bar_size="5 mins",
        duration="1 D",
        what_to_show="TRADES",
        use_rth=0,
        out_csv=out_csv,
    )

    res = fetch_historical_bars(cfg)
    print("IBKR_FETCH_RESULT:", res)
    print("CSV:", out_csv)

    # Print first lines for sanity
    print("CSV_HEAD:")
    txt = out_csv.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in txt[:6]:
        print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
