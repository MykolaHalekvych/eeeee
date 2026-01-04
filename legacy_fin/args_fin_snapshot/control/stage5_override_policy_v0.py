# args/control/stage5_override_policy_v0.py
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence, Tuple

CONTROL_STATE_PATH_CANDIDATES: Tuple[Path, ...] = (
    Path("args") / "control" / "control_state.json",
    Path("args") / "control_state.json",
    Path("control_state.json"),
)

STOP_FLAG_PATH_CANDIDATES: Tuple[Path, ...] = (
    Path("args") / "control" / "stop.flag",
    Path("stop.flag"),
)

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso_dt_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def resolve_control_state_path(explicit_path: str | Path | None = None) -> Optional[Path]:
    if explicit_path is not None:
        p = Path(explicit_path)
        return p if p.exists() else None

    env = os.getenv("ARGS_CONTROL_STATE_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return p

    for p in CONTROL_STATE_PATH_CANDIDATES:
        if p.exists():
            return p
    return None

def detect_stop_flag(control_state: Mapping[str, Any] | None = None) -> tuple[bool, Optional[Path]]:
    # 1) explicit path in control_state (optional)
    if control_state:
        p_raw = control_state.get("stop_flag_path") or control_state.get("STOP_FLAG_PATH")
        if isinstance(p_raw, str) and p_raw.strip():
            p = Path(p_raw)
            if p.exists():
                return True, p

    # 2) env override
    env = os.getenv("ARGS_STOP_FLAG_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return True, p

    # 3) common candidates
    for p in STOP_FLAG_PATH_CANDIDATES:
        if p.exists():
            return True, p

    return False, None

def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def update_control_state_json(
    path: Path,
    mutator: Callable[[MutableMapping[str, Any]], bool],
) -> bool:
    """
    Returns True if mutation applied and file was rewritten.
    """
    try:
        data = _read_json(path)
    except FileNotFoundError:
        return False

    if not isinstance(data, dict):
        return False

    changed = mutator(data)
    if not changed:
        return False

    _atomic_write_json(path, data)
    return True

@dataclass(frozen=True, slots=True)
class Stage5TestOverrideConfig:
    enabled: bool
    autoreset: bool
    expires_at_utc: Optional[datetime]

    @property
    def active(self) -> bool:
        if not self.enabled:
            return False
        if self.expires_at_utc is None:
            return True
        return _now_utc() < self.expires_at_utc

def load_stage5_test_override_config(control_state: Mapping[str, Any]) -> Stage5TestOverrideConfig:
    enabled = bool(control_state.get("stage5_test_override", False))
    autoreset = bool(control_state.get("stage5_test_override_autoreset", True))

    expires_raw = (
        control_state.get("stage5_test_override_expires_at_utc")
        or control_state.get("stage5_test_override_expires_utc")
        or control_state.get("stage5_test_override_until_utc")
    )
    expires_at_utc = _parse_iso_dt_utc(expires_raw)

    return Stage5TestOverrideConfig(
        enabled=enabled,
        autoreset=autoreset,
        expires_at_utc=expires_at_utc,
    )

def try_autoreset_stage5_test_override(control_state_path: Optional[Path], *, reason: str) -> bool:
    """
    Best-effort. If file missing or already false -> returns False.
    Writes audit fields into control_state.json for traceability.
    """
    if control_state_path is None:
        return False

    def _mut(d: MutableMapping[str, Any]) -> bool:
        if d.get("stage5_test_override") is not True:
            return False
        d["stage5_test_override"] = False
        d["stage5_test_override_last_autoreset_at_utc"] = _now_utc().isoformat()
        d["stage5_test_override_last_autoreset_reason"] = reason
        return True

    try:
        return update_control_state_json(control_state_path, _mut)
    except Exception:
        # we do not want autoreset failures to break the run
        return False
