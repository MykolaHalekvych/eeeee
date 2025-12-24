from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

import streamlit as st


REPO_ROOT = Path(__file__).resolve().parents[2]
PY = ["py", "-3.11"]


def run_cmd(args: List[str], cwd: Path = REPO_ROOT) -> Tuple[int, str]:
    """Run command and capture combined stdout/stderr."""
    try:
        p = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            shell=False,
        )
        out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        return p.returncode, out.strip()
    except Exception as e:
        return 99, f"{type(e).__name__}: {e}"


def tail_lines(path: Path, n: int) -> str:
    if not path.exists():
        return f"(missing) {path}"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def list_logs() -> List[Path]:
    d = REPO_ROOT / "args" / "logs"
    if not d.exists():
        return []
    return sorted([p for p in d.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)


def git_porcelain() -> Tuple[int, str]:
    return run_cmd(["git", "status", "--porcelain"], cwd=REPO_ROOT)


def header_status():
    st.title("ARGS Core v1 — Control Panel")
    st.caption(f"Repo: {REPO_ROOT}")

    code, out = run_cmd(["git", "--version"], cwd=REPO_ROOT)
    if code == 0:
        st.success(out)
    else:
        st.warning("Git not available in PATH for this session.")

    code, out = git_porcelain()
    if code == 0 and out.strip() == "":
        st.success("Git status: working tree clean")
    elif code == 0:
        st.warning("Git status: changes present")
        st.code(out)
    else:
        st.info("Git status: unavailable")


def run_sanity_suite() -> Dict[str, Tuple[int, str]]:
    suite = {
        "demo_regression": PY + ["-m", "args.demo.demo_regression"],
        "demo_policy_diff": PY + ["-m", "args.demo.demo_policy_diff"],
        "demo_replay": PY + ["-m", "args.demo.demo_replay"],
        "demo_meta_audit": PY + ["-m", "args.demo.demo_meta_audit"],
    }
    results: Dict[str, Tuple[int, str]] = {}
    for name, cmd in suite.items():
        code, out = run_cmd(cmd)
        results[name] = (code, out)
    return results


def run_negative_test() -> Tuple[int, str]:
    return run_cmd(PY + ["-m", "args.demo.demo_meta_audit_negative"])


def run_checkpoint(tag: str) -> Tuple[int, str]:
    script = REPO_ROOT / "scripts" / "checkpoint.ps1"
    if not script.exists():
        return 98, f"Missing script: {script}"
    cmd = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-Tag",
        tag,
    ]
    return run_cmd(cmd, cwd=REPO_ROOT)


def main():
    st.set_page_config(page_title="ARGS Control Panel", layout="wide")
    header_status()

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Actions")

        if st.button("Run sanity suite (regression/diff/replay/meta_audit)"):
            st.session_state["sanity_ts"] = _dt.datetime.now().isoformat(timespec="seconds")
            st.session_state["sanity_results"] = run_sanity_suite()

        if st.button("Run Meta Audit negative test (UNKNOWN→ALLOW, expect FAIL)"):
            st.session_state["neg_ts"] = _dt.datetime.now().isoformat(timespec="seconds")
            st.session_state["neg_result"] = run_negative_test()

        tag = st.text_input("Checkpoint tag", value="ui")
        if st.button("Run checkpoint.ps1"):
            st.session_state["chk_ts"] = _dt.datetime.now().isoformat(timespec="seconds")
            st.session_state["chk_result"] = run_checkpoint(tag)

        st.info("Note: demo_ma_eval appends to args/data/events.jsonl. Not run by default in UI.")

    with col2:
        st.subheader("Results")

        if "sanity_results" in st.session_state:
            st.write(f"Sanity run: {st.session_state.get('sanity_ts', '')}")
            for name, (code, out) in st.session_state["sanity_results"].items():
                if code == 0:
                    st.success(f"{name}: OK")
                else:
                    st.error(f"{name}: FAIL (code={code})")
                with st.expander(f"Output: {name}", expanded=False):
                    st.code(out or "(no output)")

        if "neg_result" in st.session_state:
            code, out = st.session_state["neg_result"]
            st.write(f"Negative test: {st.session_state.get('neg_ts', '')}")
            if code == 2:
                st.success("Negative test: FAIL detected as expected (exit_code=2).")
            elif code == 0:
                st.error("Negative test: expected exit_code=2 but got 0.")
            else:
                st.error(f"Negative test: error (code={code}).")
            st.code(out or "(no output)")

        if "chk_result" in st.session_state:
            code, out = st.session_state["chk_result"]
            st.write(f"Checkpoint: {st.session_state.get('chk_ts', '')}")
            if code == 0:
                st.success("Checkpoint OK")
            else:
                st.error(f"Checkpoint FAIL (code={code})")
            st.code(out or "(no output)")

    st.divider()

    st.subheader("Logs & Evidence")
    logs = list_logs()
    if logs:
        pick = st.selectbox("Select log file", logs, format_func=lambda p: p.name)
        n = st.slider("Tail lines", min_value=20, max_value=400, value=120, step=20)
        st.code(tail_lines(pick, n))
    else:
        st.info("No logs found in args/logs yet.")

    st.subheader("events.jsonl (tail)")
    events_path = REPO_ROOT / "args" / "data" / "events.jsonl"
    n2 = st.slider("Tail lines (events)", min_value=10, max_value=200, value=40, step=10)
    st.code(tail_lines(events_path, n2))


if __name__ == "__main__":
    main()
