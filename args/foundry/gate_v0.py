from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from args.foundry.guard_v0 import find_repo_root, require_engine_repo

EXIT_OK = 0
EXIT_EVAL_FAIL = 1
EXIT_INFRA = 2

SCOPE_DEFAULT = [
    "args",
    "manifests",
    "packs",
    "templates",
    "scripts",
    "control_plane.json",
    ".gitignore",
    ".gitattributes",
]

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

TEXT_EXTS = {".py", ".json", ".md", ".ps1", ".yml", ".yaml", ".txt", ".toml", ".ini", ".cfg"}

SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai_like", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("generic_secret", re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
]

def dump(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def rel(repo_root: Path, p: Path) -> str:
    return p.resolve().relative_to(repo_root.resolve()).as_posix()

def list_scope_files(repo_root: Path, scope: List[str]) -> List[Path]:
    files: List[Path] = []

    def should_exclude_dir(d: Path) -> bool:
        return d.name in EXCLUDE_DIRS

    for item in scope:
        p = (repo_root / item)
        if p.is_file():
            files.append(p)
            continue
        if p.is_dir():
            for x in p.rglob("*"):
                if x.is_dir():
                    if should_exclude_dir(x):
                        # prune by skipping: rglob can't prune directly, so just continue
                        continue
                    continue
                # exclude if any parent dir excluded
                if any(part in EXCLUDE_DIRS for part in x.parts):
                    continue
                files.append(x)

    # unique + sorted by relative path
    uniq = {str(f.resolve()): f for f in files if f.exists() and f.is_file()}
    out = sorted(uniq.values(), key=lambda p: rel(repo_root, p))
    return out

def py_compile_check(repo_root: Path) -> Tuple[bool, List[str]]:
    # Compile only engine python code (not legacy)
    targets = [
        repo_root / "args" / "__init__.py",
        *(repo_root / "args" / "foundry").glob("*.py"),
    ]
    errs: List[str] = []
    for t in targets:
        if not t.exists():
            continue
        r = subprocess.run([sys.executable, "-m", "py_compile", str(t)], capture_output=True, text=True)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            errs.append(f"{rel(repo_root, t)} :: {msg[:400]}")
    return (len(errs) == 0), errs

def ruff_available() -> bool:
    return importlib.util.find_spec("ruff") is not None

def ruff_check(repo_root: Path, files: List[Path]) -> Tuple[int, str]:
    # Run ruff only on python files under args/foundry (deterministic scope)
    py_files = [rel(repo_root, p) for p in files if p.suffix == ".py" and "legacy_fin" not in p.parts]
    # focus on engine code only
    py_files = [p for p in py_files if p.startswith("args/")]

    if not py_files:
        return 0, ""

    cmd = [sys.executable, "-m", "ruff", "check", *py_files]
    r = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    # normalize line endings for determinism
    out = out.replace("\r\n", "\n").strip()
    return r.returncode, out

def secrets_scan(repo_root: Path, files: List[Path]) -> Tuple[bool, List[Dict[str, Any]]]:
    findings: List[Dict[str, Any]] = []
    for p in files:
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        # ignore large binaries by extension filter above; still protect size
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            # infra but don't crash; treat as finding of infra type
            findings.append({"type": "read_error", "path": rel(repo_root, p)})
            continue

        # simple allowlist: ignore obvious placeholders
        if "PLACEHOLDER" in text or "example" in text.lower():
            pass

        for name, rx in SECRET_PATTERNS:
            for m in rx.finditer(text):
                # compute line number
                upto = text[: m.start()]
                line = upto.count("\n") + 1
                snippet = m.group(0)[:60]
                findings.append({
                    "type": name,
                    "path": rel(repo_root, p),
                    "line": line,
                    "match": snippet,
                })
                # do not spam same file too much
                if len([f for f in findings if f.get("path") == rel(repo_root, p)]) >= 10:
                    break
    ok = len(findings) == 0
    return ok, findings

def run_gate(repo_root: Path, scope: List[str] | None = None) -> Tuple[int, Dict[str, Any]]:
    scope = scope or SCOPE_DEFAULT
    files = list_scope_files(repo_root, scope)
    files_rel = [rel(repo_root, p) for p in files]

    checks: List[Dict[str, Any]] = []

    # 1) py_compile
    ok_compile, compile_errs = py_compile_check(repo_root)
    checks.append({
        "id": "py_compile",
        "ok": ok_compile,
        "errors": compile_errs[:20],
        "error_count": len(compile_errs),
    })

    infra_fail = False
    eval_fail = False

    if not ok_compile:
        eval_fail = True

    # 2) ruff (infra if missing)
    if not ruff_available():
        infra_fail = True
        checks.append({
            "id": "ruff",
            "ok": False,
            "infra": True,
            "missing_tool": "ruff",
        })
        ruff_out = ""
        ruff_rc = None
    else:
        ruff_rc, ruff_out = ruff_check(repo_root, files)
        ok_ruff = (ruff_rc == 0)
        checks.append({
            "id": "ruff",
            "ok": ok_ruff,
            "rc": ruff_rc,
            "output": ruff_out[:2000],  # keep bounded
        })
        if not ok_ruff:
            eval_fail = True

    # 3) secrets scan
    ok_secrets, findings = secrets_scan(repo_root, files)
    checks.append({
        "id": "secrets_scan",
        "ok": ok_secrets,
        "finding_count": len(findings),
        "findings": findings[:30],  # bounded
    })
    if not ok_secrets:
        eval_fail = True

    summary = {
        "schema": "foundry_gate_v0",
        "ok": (not infra_fail) and (not eval_fail),
        "scope": scope,
        "scope_file_count": len(files_rel),
        "checks": checks,
        # deterministic fingerprint of scope inputs (does not include timestamps)
        "scope_sha256": hashlib.sha256(("\n".join(files_rel)).encode("utf-8")).hexdigest(),
    }

    if infra_fail:
        return EXIT_INFRA, summary
    if eval_fail:
        return EXIT_EVAL_FAIL, summary
    return EXIT_OK, summary

def main() -> int:
    ap = argparse.ArgumentParser(prog="foundry_gate_v0")
    ap.add_argument("--control-plane", default="control_plane.json")
    ap.add_argument("--scope", nargs="*", default=None, help="override scope paths")
    args = ap.parse_args()

    repo_root = find_repo_root(Path("."))
    try:
        require_engine_repo(repo_root)
    except Exception as e:
        dump({"schema":"foundry_gate_v0","ok":False,"exit_code":EXIT_INFRA,"error":str(e)})
        return EXIT_INFRA

    # control-plane load just for fingerprint / sanity (do not enforce permissions here)
    try:
        cp_path = (repo_root / Path(args.control_plane)).resolve()
        cp_hash = sha256_file(cp_path)
    except Exception as e:
        dump({"schema":"foundry_gate_v0","ok":False,"exit_code":EXIT_INFRA,"error":f"control_plane load/hash failed: {type(e).__name__}: {e}"})
        return EXIT_INFRA

    code, summary = run_gate(repo_root, scope=args.scope)
    out = {
        "schema": "foundry_gate_v0",
        "ok": summary["ok"],
        "exit_code": code,
        "control_plane_sha256": cp_hash,
        "summary": summary,
    }
    dump(out)
    return code

if __name__ == "__main__":
    raise SystemExit(main())
