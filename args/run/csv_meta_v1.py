from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def attach_csv_meta(run_report: Dict[str, Any], csv_path: Union[str, Path]) -> None:
    try:
        p = csv_path if isinstance(csv_path, Path) else Path(str(csv_path))
    except Exception:
        return

    meta_path = p.with_suffix(".meta.json")
    if not meta_path.exists():
        return

    run_report.setdefault("inputs", {})
    run_report["inputs"]["csv_meta_path"] = str(meta_path)

    meta = _load_json(meta_path)
    if meta is not None:
        run_report["inputs"]["csv_meta"] = meta
