from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common_v0 import read_json, atomic_write_json


@dataclass(frozen=True)
class ControlPlane:
    execution_mode: str = "PAPER"
    enable_paper_execution: bool = True

    global_mode: str = "ONLY_EXITS"  # ONLY_EXITS / NORMAL
    allowlist: List[str] = field(default_factory=list)

    run_root: str = "runs"
    kill_switch_file: str = "stop.flag"
    safe_mode_file: str = "safe_mode.flag"
    cool_down_seconds: int = 30

    max_actions_per_second: float = 10.0
    max_actions_burst: int = 20

    as_model_version: Optional[str] = None

    @staticmethod
    def load(path: Path) -> "ControlPlane":
        raw = read_json(path, default={})
        return ControlPlane(
            execution_mode=str(raw.get("execution_mode", "PAPER")),
            enable_paper_execution=bool(raw.get("enable_paper_execution", True)),
            global_mode=str(
                raw.get("global_mode", raw.get("globalMode", "ONLY_EXITS"))
            ),
            allowlist=list(raw.get("allowlist", raw.get("allowList", [])) or []),
            run_root=str(raw.get("run_root", raw.get("runRoot", "runs"))),
            kill_switch_file=str(
                raw.get("kill_switch_file", raw.get("killSwitchFile", "stop.flag"))
            ),
            safe_mode_file=str(
                raw.get("safe_mode_file", raw.get("safeModeFile", "safe_mode.flag"))
            ),
            cool_down_seconds=int(
                raw.get("cool_down_seconds", raw.get("coolDownSeconds", 30))
            ),
            max_actions_per_second=float(
                raw.get("max_actions_per_second", raw.get("maxActionsPerSecond", 10.0))
            ),
            max_actions_burst=int(
                raw.get("max_actions_burst", raw.get("maxActionsBurst", 20))
            ),
            as_model_version=raw.get("as_model_version", raw.get("asModelVersion")),
        )

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> None:
        atomic_write_json(path, self.to_json())
