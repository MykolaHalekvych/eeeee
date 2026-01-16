from pathlib import Path
import sys

def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    template_path = repo / "templates" / "web_dashboard_v0" / "src" / "app.py"
    build_exe_path = repo / "args" / "foundry" / "build_exe_v0.py"

    if not template_path.exists():
        print(f"FAIL: template not found: {template_path}")
        return 1
    if not build_exe_path.exists():
        print(f"FAIL: build_exe not found: {build_exe_path}")
        return 1

    tpl = template_path.read_text(encoding="utf-8")
    if '"""' in tpl or "'''" in tpl:
        print("FAIL: template contains triple quotes (docstrings). Remove them first.")
        return 1
    if not tpl.endswith("\n"):
        tpl += "\n"

    text = build_exe_path.read_text(encoding="utf-8")

    start_pat = 'FALLBACK_WEB_DASHBOARD_APP_PY = r"""'
    start_idx = text.find(start_pat)
    if start_idx < 0:
        print("FAIL: start marker not found in build_exe_v0.py")
        return 1

    start_line_end = text.find("\n", start_idx)
    if start_line_end < 0:
        print("FAIL: start marker line has no newline")
        return 1
    start_line_end += 1

    # Preferred: close triple quotes then blank line then def ensure_entrypoint_overlay
    end_pat = '"""\n\n\ndef ensure_entrypoint_overlay'
    end_idx = text.find(end_pat, start_line_end)

    if end_idx < 0:
        # Fallback: locate def, then search backward for the last '\n"""' before it
        def_pat = "\ndef ensure_entrypoint_overlay"
        def_idx = text.find(def_pat, start_line_end)
        if def_idx < 0:
            print("FAIL: cannot locate ensure_entrypoint_overlay")
            return 1

        end_idx = text.rfind('\n"""', start_line_end, def_idx)
        if end_idx < 0:
            print("FAIL: cannot locate closing triple quotes before ensure_entrypoint_overlay")
            return 1
        end_idx = end_idx + 1  # point at first quote

    prefix = text[:start_line_end]
    suffix = text[end_idx:]

    new_text = prefix + tpl + suffix
    build_exe_path.write_text(new_text, encoding="utf-8", newline="\n")

    print("OK: fallback synced")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
