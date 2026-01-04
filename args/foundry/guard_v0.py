from __future__ import annotations
from pathlib import Path

ENG_MARKER = ".args_engine_repo"

def find_repo_root(start: Path) -> Path:
    p = start.resolve()
    if p.is_file():
        p = p.parent
    for _ in range(80):
        if (p / ENG_MARKER).exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return start.resolve() if start.is_dir() else start.parent.resolve()

def require_engine_repo(repo_root: Path) -> None:
    if not (repo_root / ENG_MARKER).exists():
        raise RuntimeError(f"ENG guard failed: missing {ENG_MARKER} at repo root: {repo_root}")
