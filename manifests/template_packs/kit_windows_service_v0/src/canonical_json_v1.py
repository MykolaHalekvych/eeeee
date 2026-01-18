from __future__ import annotations

import json
from typing import Any


def dumps_canonical(obj: Any, *, indent: int | None = 2) -> str:
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ": "),
    )
