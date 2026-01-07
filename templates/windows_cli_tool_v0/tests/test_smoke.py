from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def test_help_exit_zero() -> None:
    root = Path(__file__).resolve().parents[1]
    main_py = root / "src" / "main.py"
    cp = run([sys.executable, str(main_py), "--help"])
    assert cp.returncode == 0


def test_hello_exit_zero() -> None:
    root = Path(__file__).resolve().parents[1]
    main_py = root / "src" / "main.py"
    cp = run([sys.executable, str(main_py), "hello", "--name", "kolya"])
    assert cp.returncode == 0
    assert "hello, kolya" in (cp.stdout or "")
