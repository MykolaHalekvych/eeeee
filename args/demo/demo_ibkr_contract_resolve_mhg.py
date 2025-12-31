from __future__ import annotations

import json
from typing import Any, Dict

from args.ibkr.ibkr_contract_resolver_v1 import (
    IbkrConn,
    load_ibkr_connection,
    resolve_copper_contract_unattended,
)


def main() -> int:
    # load_ibkr_connection now returns IbkrConn (attribute access)
    base = load_ibkr_connection()

    conn = IbkrConn(
        host=str(base.host),
        port=int(base.port),
        client_id=int(base.client_id),
        timeout_s=float(base.timeout_s),
        wait_s=float(base.wait_s),
    )

    res: Dict[str, Any] = resolve_copper_contract_unattended(
        symbol="MHG",
        conn=conn,
        sec_type="FUT",
        exchange="COMEX",
        currency="USD",
    )

    print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
