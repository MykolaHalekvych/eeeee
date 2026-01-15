from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    build_exe = repo / "args" / "foundry" / "build_exe_v0.py"
    template_app = repo / "templates" / "web_dashboard_v0" / "src" / "app.py"

    src = build_exe.read_text(encoding="utf-8").replace("\r\n", "\n")
    app = template_app.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n") + "\n"

    required = 'STDOUT_MODE = "win_writefile_or_oswrite_v2"'
    if required not in app:
        print("FAIL: required STDOUT_MODE line missing in template app.py")
        return 1

    key = "FALLBACK_WEB_DASHBOARD_APP_PY"
    k = src.find(key)
    if k < 0:
        print("FAIL: FALLBACK_WEB_DASHBOARD_APP_PY not found")
        return 1

    open_q = src.find('"""', k)
    if open_q < 0:
        print("FAIL: opening triple-quote not found")
        return 1

    open_end = open_q + 3
    close_q = src.find('"""', open_end)
    if close_q < 0:
        print("FAIL: closing triple-quote not found")
        return 1

    new_inner = "\n" + app.rstrip("\n") + "\n"
    out = src[:open_end] + new_inner + src[close_q:]

    build_exe.write_text(out, encoding="utf-8")
    print("OK: fallback synced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
