from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass(frozen=True)
class RetentionPolicyV1:
    artifact_root: str

    releases_dir: str
    keep_last_per_product: int
    pinned_release_ids: List[str]

    runs_dir: str
    keep_last_pass: int
    keep_days_fail_infra: int
    keep_run_ids: List[str]

    tmp_dir: str
    max_age_days: int

    require_paths_under_artifact_root: bool
    dryrun_default: bool


def _req(obj: Dict[str, Any], k: str) -> Any:
    if k not in obj:
        raise ValueError(f"missing_required_field:{k}")
    return obj[k]


def load_retention_policy_v1(policy_path: str) -> RetentionPolicyV1:
    p = Path(policy_path)
    if not p.exists():
        raise ValueError(f"policy_not_found:{policy_path}")

    data = json.loads(p.read_text(encoding="utf-8"))

    if data.get("schema") != "retention_policy_v1":
        raise ValueError("bad_schema")
    if int(data.get("version", 0)) != 1:
        raise ValueError("bad_version")

    dist = _req(data, "dist")
    runs = _req(data, "runs")
    tmp = _req(data, "tmp")
    safety = data.get("safety", {}) or {}

    return RetentionPolicyV1(
        artifact_root=str(_req(data, "artifact_root")),

        releases_dir=str(_req(dist, "releases_dir")),
        keep_last_per_product=int(_req(dist, "keep_last_per_product")),
        pinned_release_ids=list(dist.get("pinned_release_ids", []) or []),

        runs_dir=str(_req(runs, "runs_dir")),
        keep_last_pass=int(_req(runs, "keep_last_pass")),
        keep_days_fail_infra=int(_req(runs, "keep_days_fail_infra")),
        keep_run_ids=list(runs.get("keep_run_ids", []) or []),

        tmp_dir=str(_req(tmp, "tmp_dir")),
        max_age_days=int(_req(tmp, "max_age_days")),

        require_paths_under_artifact_root=bool(safety.get("require_paths_under_artifact_root", True)),
        dryrun_default=bool(safety.get("dryrun_default", True)),
    )