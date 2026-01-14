
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.foundry.guard_v0 import find_repo_root, require_engine_repo

# Contract Standard v0
EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2

SCHEMA = "foundry_gate_v0"

SCOPE_DEFAULT = [
    "args",  # NOTE: box-ready scan excludes args/data automatically
    "manifests",
    "packs",
    "templates",
    "scripts",
    "control_plane.json",
    ".gitignore",
    ".gitattributes",
]

# Generic directory excludes (name-based)
EXCLUDE_DIRS = {
    ".git",
    "legacy_fin",
    "dist",
    "runs",
    "__pycache__",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
}

# Box-ready hard exclude prefix: args/data (runtime payloads explode scope size)
EXCLUDE_PREFIXES = [
    ("args", "data"),
]

TEXT_EXTS = {
    ".py",
    ".json",
    ".md",
    ".ps1",
    ".yml",
    ".yaml",
    ".txt",
    ".toml",
    ".ini",
    ".cfg",
}

SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai_like", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("generic_secret", re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _rand_hex(n: int) -> str:
    import random

    return "".join(f"{random.randrange(16):x}" for _ in range(n))


def _dump_one_json(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # deterministic newlines
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(repo_root: Path, p: Path) -> str:
    rr = repo_root.resolve()
    pr = p.resolve()
    try:
        return pr.relative_to(rr).as_posix()
    except ValueError:
        # Outside repo_root — keep stable key without crashing
        return pr.as_posix()


def _is_excluded_prefix(repo_root: Path, p: Path) -> bool:
    """Exclude by repo-relative prefix, e.g. args/data."""
    try:
        rp = p.resolve().relative_to(repo_root.resolve())
    except Exception:
        return False
    parts = rp.parts
    for pref in EXCLUDE_PREFIXES:
        if len(parts) >= len(pref) and tuple(parts[: len(pref)]) == pref:
            return True
    return False


def list_scope_files(repo_root: Path, scope: List[str]) -> Tuple[List[Path], List[str]]:
    """
    Box-ready walker with pruning.
    - Excludes args/data (runtime payloads) hard.
    - Excludes EXCLUDE_DIRS by name.
    """
    rr = repo_root.resolve()
    files: List[Path] = []
    missing: List[str] = []

    for item in scope:
        p = (rr / item)

        if not p.exists():
            missing.append(item)
            continue

        if p.is_file():
            if not _is_excluded_prefix(rr, p):
                files.append(p)
            continue

        if not p.is_dir():
            continue

        # Hard skip if scope points directly into excluded prefix
        if _is_excluded_prefix(rr, p):
            continue

        # Walk with pruning
        for dirpath, dirnames, filenames in os.walk(str(p), topdown=True, followlinks=False):
            dp = Path(dirpath)

            # prune excluded dirs by name
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

            # special prune: if we're at repo_root/args, do not descend into args/data
            try:
                if dp.resolve() == (rr / "args").resolve():
                    dirnames[:] = [d for d in dirnames if d != "data"]
            except Exception:
                # ignore resolution issues
                pass

            # if we ever land inside excluded prefix, stop descending
            if _is_excluded_prefix(rr, dp):
                dirnames[:] = []
                continue

            for fn in filenames:
                fp = dp / fn
                # skip any file under excluded prefix
                if _is_excluded_prefix(rr, fp):
                    continue
                # quick name-based parent exclude safety
                if any(part in EXCLUDE_DIRS for part in fp.parts):
                    continue
                files.append(fp)

    # unique + sorted by relative path
    uniq = {str(f.resolve()): f for f in files if f.exists() and f.is_file()}
    out = sorted(uniq.values(), key=lambda x: rel(rr, x))
    return out, missing


def _write_step(out_dir: Path, step_name: str, stdout_text: str, stderr_text: str, step_summary: Dict[str, Any]) -> Dict[str, Any]:
    step_dir = out_dir / f"step_{step_name}"
    step_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = step_dir / "stdout.txt"
    stderr_path = step_dir / "stderr.txt"
    summary_path = step_dir / "summary.json"

    _write_text(stdout_path, stdout_text)
    _write_text(stderr_path, stderr_text)
    _write_json(summary_path, step_summary)

    return {
        "step": step_name,
        "out_dir": str(step_dir),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "summary_path": str(summary_path),
        "exit_code": int(step_summary.get("exit_code", EXIT_INFRA)),
        "result": step_summary.get("result", "INFRA"),
        "reason_code": step_summary.get("reason_code"),
        "ok": bool(step_summary.get("ok", False)),
    }


def py_compile_check(repo_root: Path) -> Tuple[bool, List[str], str]:
    # Compile only engine python code (not legacy)
    targets = [
        repo_root / "args" / "__init__.py",
        *(repo_root / "args" / "foundry").glob("*.py"),
    ]
    errs: List[str] = []
    compiled: List[str] = []

    for t in targets:
        if not t.exists():
            continue
        compiled.append(rel(repo_root, t))
        r = subprocess.run([sys.executable, "-m", "py_compile", str(t)], capture_output=True, text=True)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            errs.append(f"{rel(repo_root, t)} :: {msg[:400]}")

    stdout = "\n".join(compiled).strip()
    return (len(errs) == 0), errs, stdout


def ruff_available() -> bool:
    return importlib.util.find_spec("ruff") is not None


def ruff_check(repo_root: Path) -> Tuple[int, str]:
    # Box-ready: avoid giant file lists and avoid scanning args/data by limiting to args/foundry
    cmd = [sys.executable, "-m", "ruff", "check", "args/foundry"]
    r = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    out = out.replace("\r\n", "\n").strip()
    return r.returncode, out


def secrets_scan(repo_root: Path, files: List[Path]) -> Tuple[bool, List[Dict[str, Any]], int]:
    findings: List[Dict[str, Any]] = []
    read_errors = 0

    for p in files:
        if p.suffix.lower() not in TEXT_EXTS:
            continue

        # protect size
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            read_errors += 1
            findings.append({"type": "read_error", "path": rel(repo_root, p)})
            continue

        for name, rx in SECRET_PATTERNS:
            for m in rx.finditer(text):
                upto = text[: m.start()]
                line = upto.count("\n") + 1
                snippet = m.group(0)[:60]
                findings.append(
                    {
                        "type": name,
                        "path": rel(repo_root, p),
                        "line": line,
                        "match": snippet,
                    }
                )
                # bound spam per file
                if len([f for f in findings if f.get("path") == rel(repo_root, p)]) >= 10:
                    break

    ok = (len([f for f in findings if f.get("type") != "read_error"]) == 0) and (read_errors == 0)
    return ok, findings, read_errors


def run_gate(repo_root: Path, out_dir: Path, scope: List[str]) -> Tuple[int, Dict[str, Any]]:
    ts = _utc_now_iso()

    steps: List[Dict[str, Any]] = []
    checks: List[Dict[str, Any]] = []
    reason_codes: List[str] = []

    infra_fail = False
    eval_fail = False

    # Step 0: scope listing (pruned, box-ready)
    try:
        files, missing = list_scope_files(repo_root, scope)
        files_rel = [rel(repo_root, p) for p in files]
        scope_sha = hashlib.sha256(("\n".join(files_rel)).encode("utf-8")).hexdigest()
        step0_summary = {
            "schema": f"{SCHEMA}.step.scope",
            "ts_utc": ts,
            "ok": True,
            "exit_code": EXIT_OK,
            "result": "PASS",
            "reason_code": "PASS",
            "scope": scope,
            "scope_missing": missing,
            "scope_file_count": len(files_rel),
            "scope_sha256": scope_sha,
        }
        step0_stdout = json.dumps(
            {
                "scope_file_count": len(files_rel),
                "scope_missing": missing,
                "scope_sha256": scope_sha,
            },
            ensure_ascii=False,
        )
        steps.append(_write_step(out_dir, "scope", step0_stdout + "\n", "", step0_summary))
    except KeyboardInterrupt:
        step0_summary = {
            "schema": f"{SCHEMA}.step.scope",
            "ts_utc": ts,
            "ok": False,
            "exit_code": EXIT_INFRA,
            "result": "INFRA",
            "reason_code": "INFRA_INTERRUPTED",
            "error": "KeyboardInterrupt",
        }
        steps.append(_write_step(out_dir, "scope", "", "KeyboardInterrupt\n", step0_summary))
        infra_fail = True
        reason_codes.append("INFRA_INTERRUPTED")
        # minimal summary
        summary = {
            "schema": SCHEMA,
            "ok": False,
            "ts_utc": ts,
            "scope": scope,
            "scope_file_count": 0,
            "checks": [],
            "scope_sha256": "",
            "steps": steps,
            "reason_code": "|".join(reason_codes) if reason_codes else "INFRA_INTERRUPTED",
        }
        return EXIT_INFRA, summary
    except Exception as e:
        step0_summary = {
            "schema": f"{SCHEMA}.step.scope",
            "ts_utc": ts,
            "ok": False,
            "exit_code": EXIT_INFRA,
            "result": "INFRA",
            "reason_code": "INFRA_SCOPE_LIST",
            "error": f"{type(e).__name__}: {e}",
        }
        steps.append(_write_step(out_dir, "scope", "", step0_summary["error"] + "\n", step0_summary))
        infra_fail = True
        reason_codes.append("INFRA_SCOPE_LIST")
        summary = {
            "schema": SCHEMA,
            "ok": False,
            "ts_utc": ts,
            "scope": scope,
            "scope_file_count": 0,
            "checks": [],
            "scope_sha256": "",
            "steps": steps,
            "reason_code": "|".join(reason_codes),
        }
        return EXIT_INFRA, summary

    # 1) py_compile
    ok_compile, compile_errs, compile_stdout = py_compile_check(repo_root)
    checks.append(
        {
            "id": "py_compile",
            "ok": ok_compile,
            "errors": compile_errs[:20],
            "error_count": len(compile_errs),
        }
    )
    if not ok_compile:
        eval_fail = True
        reason_codes.append("FAIL_PY_COMPILE")

    step1_summary = {
        "schema": f"{SCHEMA}.step.py_compile",
        "ts_utc": ts,
        "ok": ok_compile,
        "exit_code": EXIT_OK if ok_compile else EXIT_EVAL_FAIL,
        "result": "PASS" if ok_compile else "FAIL",
        "reason_code": "PASS" if ok_compile else "FAIL_PY_COMPILE",
        "error_count": len(compile_errs),
    }
    step1_stderr = ("\n".join(compile_errs)).strip()
    steps.append(_write_step(out_dir, "py_compile", compile_stdout.strip() + ("\n" if compile_stdout else ""), step1_stderr + ("\n" if step1_stderr else ""), step1_summary))

    # 2) ruff
    if not ruff_available():
        infra_fail = True
        reason_codes.append("INFRA_MISSING_RUFF")
        checks.append({"id": "ruff", "ok": False, "infra": True, "missing_tool": "ruff"})
        step2_summary = {
            "schema": f"{SCHEMA}.step.ruff",
            "ts_utc": ts,
            "ok": False,
            "exit_code": EXIT_INFRA,
            "result": "INFRA",
            "reason_code": "INFRA_MISSING_RUFF",
        }
        steps.append(_write_step(out_dir, "ruff", "", "ruff missing\n", step2_summary))
    else:
        ruff_rc, ruff_out = ruff_check(repo_root)
        ok_ruff = (ruff_rc == 0)
        checks.append({"id": "ruff", "ok": ok_ruff, "rc": ruff_rc, "output": ruff_out[:2000]})
        if not ok_ruff:
            eval_fail = True
            reason_codes.append("FAIL_RUFF")

        step2_summary = {
            "schema": f"{SCHEMA}.step.ruff",
            "ts_utc": ts,
            "ok": ok_ruff,
            "exit_code": EXIT_OK if ok_ruff else EXIT_EVAL_FAIL,
            "result": "PASS" if ok_ruff else "FAIL",
            "reason_code": "PASS" if ok_ruff else "FAIL_RUFF",
            "rc": ruff_rc,
        }
        steps.append(_write_step(out_dir, "ruff", (ruff_out or "").strip() + ("\n" if ruff_out else ""), "", step2_summary))

    # 3) secrets scan
    ok_secrets, findings, read_errors = secrets_scan(repo_root, files)

    # read errors are infra, not eval
    if read_errors > 0:
        infra_fail = True
        reason_codes.append("INFRA_SECRETS_READ_ERROR")

    checks.append({"id": "secrets_scan", "ok": ok_secrets, "finding_count": len(findings), "findings": findings[:30]})

    if not ok_secrets and read_errors == 0:
        eval_fail = True
        reason_codes.append("FAIL_SECRETS_SCAN")

    step3_summary = {
        "schema": f"{SCHEMA}.step.secrets_scan",
        "ts_utc": ts,
        "ok": ok_secrets,
        "exit_code": EXIT_OK if ok_secrets else (EXIT_INFRA if read_errors > 0 else EXIT_EVAL_FAIL),
        "result": "PASS" if ok_secrets else ("INFRA" if read_errors > 0 else "FAIL"),
        "reason_code": "PASS" if ok_secrets else ("INFRA_SECRETS_READ_ERROR" if read_errors > 0 else "FAIL_SECRETS_SCAN"),
        "finding_count": len(findings),
        "read_error_count": read_errors,
    }
    # keep bounded but useful in stdout
    step3_stdout = json.dumps({"finding_count": len(findings), "read_error_count": read_errors, "findings": findings[:30]}, ensure_ascii=False)
    steps.append(_write_step(out_dir, "secrets", step3_stdout + "\n", "", step3_summary))

    # Final summary (backward-compatible shape)
    ok_all = (not infra_fail) and (not eval_fail)
    if ok_all:
        reason_code = "PASS"
        code = EXIT_OK
    elif infra_fail:
        reason_code = "|".join(reason_codes) if reason_codes else "INFRA"
        code = EXIT_INFRA
    else:
        reason_code = "|".join(reason_codes) if reason_codes else "FAIL"
        code = EXIT_EVAL_FAIL

    summary = {
        "schema": SCHEMA,
        "ok": ok_all,
        "ts_utc": ts,
        "reason_code": reason_code,
        "scope": scope,
        "scope_file_count": len(files),
        "checks": checks,
        "scope_sha256": steps[0].get("reason_code") and (json.loads(Path(steps[0]["stdout_path"]).read_text(encoding="utf-8", errors="ignore") or "{}").get("scope_sha256") or "") or "",
        "steps": steps,
    }

    return code, summary


def main() -> int:
    ap = argparse.ArgumentParser(prog=SCHEMA)
    ap.add_argument("--control-plane", default="control_plane.json")
    ap.add_argument("--scope", nargs="*", default=None, help="override scope paths")
    ap.add_argument("--out-dir", default="", help="output directory (default: args/data/gates/gate_v0/<run_id>)")
    ap.add_argument("--run-id", default="", help="optional run id for default out-dir naming")
    args = ap.parse_args()

    ts = _utc_now_iso()

    # Resolve repo root
    try:
        repo_root = find_repo_root(Path("."))
    except Exception:
        repo_root = Path(".").resolve()

    # Prepare out_dir early (contract requires summary.json)
    run_id = args.run_id.strip() or f"GATE_{_utc_now_id()}_{_rand_hex(8)}"
    if args.out_dir.strip():
        od = Path(args.out_dir)
        out_dir = od if od.is_absolute() else (repo_root / od)
    else:
        out_dir = repo_root / "args" / "data" / "gates" / "gate_v0" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Base output skeleton
    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "ts_utc": ts,
        "ok": False,
        "exit_code": EXIT_INFRA,
        "reason_code": "INFRA_INIT",
        "repo": str(repo_root),
        "run_id": run_id,
        "out_dir": str(out_dir),
        "control_plane_sha256": None,
        "summary": None,
    }

    try:
        require_engine_repo(repo_root)
    except Exception as e:
        out["reason_code"] = "INFRA_NOT_ENGINE_REPO"
        out["error"] = str(e)
        _write_json(out_dir / "summary.json", out)
        _dump_one_json(out)
        return EXIT_INFRA

    # control-plane load/hash
    try:
        cp_path = (repo_root / Path(args.control_plane)).resolve()
        out["control_plane_sha256"] = sha256_file(cp_path)
    except KeyboardInterrupt:
        out["reason_code"] = "INFRA_INTERRUPTED"
        out["error"] = "KeyboardInterrupt"
        _write_json(out_dir / "summary.json", out)
        _dump_one_json(out)
        return EXIT_INFRA
    except Exception as e:
        out["reason_code"] = "INFRA_CONTROL_PLANE_LOAD"
        out["error"] = f"control_plane load/hash failed: {type(e).__name__}: {e}"
        _write_json(out_dir / "summary.json", out)
        _dump_one_json(out)
        return EXIT_INFRA

    # Run gate (contract: catch KeyboardInterrupt and still emit JSON)
    try:
        scope = args.scope if args.scope else SCOPE_DEFAULT
        code, summary = run_gate(repo_root, out_dir, scope)
        out["exit_code"] = code
        out["summary"] = summary
        out["ok"] = bool(summary.get("ok", False))
        out["reason_code"] = summary.get("reason_code", "PASS" if out["ok"] else "FAIL")
    except KeyboardInterrupt:
        out["exit_code"] = EXIT_INFRA
        out["ok"] = False
        out["reason_code"] = "INFRA_INTERRUPTED"
        out["error"] = "KeyboardInterrupt"
    except Exception as e:
        out["exit_code"] = EXIT_INFRA
        out["ok"] = False
        out["reason_code"] = "INFRA_GATE_EXCEPTION"
        out["error"] = f"{type(e).__name__}: {e}"

    _write_json(out_dir / "summary.json", out)
    _dump_one_json(out)
    return int(out["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
