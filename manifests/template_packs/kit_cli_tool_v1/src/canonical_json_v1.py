from __future__ import annotations

import json
from typing import Any


def dumps_canonical(obj: Any, *, indent: int | None = 2) -> str:
    """
    Deterministic JSON encoding:
    - stable key order
    - stable separators
    - utf-8 friendly
    """
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ": "),
    )


def loads_json(text: str) -> Any:
    return json.loads(text)
