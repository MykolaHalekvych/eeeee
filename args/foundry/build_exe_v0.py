
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Tuple

SCHEMA = "build_exe_v0"


# ---------------------------
# Common helpers
# ---------------------------

def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "cmd": cmd,
        "rc": int(p.returncode),
        "stdout": p.stdout or "",
        "stderr": p.stderr or "",
    }


def emit_and_exit(payload: dict[str, Any], code: int) -> int:
    payload["schema"] = payload.get("schema", SCHEMA)
    payload["exit_code"] = int(code)
    payload["ok"] = (int(code) == 0)
    s = json.dumps(payload, ensure_ascii=False)
    sys.stdout.write(s)
    sys.stdout.flush()
    return int(code)


def py_compile_tree(root: Path) -> dict[str, Any]:
    import py_compile

    py_files = sorted(root.rglob("*.py"))
    errors: list[dict[str, str]] = []
    for f in py_files:
        try:
            py_compile.compile(str(f), doraise=True)
        except Exception as e:  # noqa: BLE001
            errors.append({"file": str(f), "error": repr(e)})
    return {"files": [str(p) for p in py_files], "errors": errors, "ok": len(errors) == 0}


@dataclass
class Product:
    product_id: str
    version: str
    template_dir: str
    entrypoint: str
    exe_name: str


def load_product(repo: Path, product_id: str) -> Product:
    manifest_path = repo / "manifests" / "products" / f"{product_id}.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"product manifest not found: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

    if data.get("product_id") != product_id:
        raise ValueError("product_id mismatch in manifest")

    return Product(
        product_id=product_id,
        version=str(data.get("version", "0.0.0")),
        template_dir=str(data["template_dir"]),
        entrypoint=str(data["entrypoint"]),
        exe_name=str(data.get("exe_name", "app.exe")),
    )


def build_runbook(exe_name: str) -> str:
    return f"""# Runbook (Release Pack v0)

## Run

- Show help:
  - `{exe_name} --help`

- Version:
  - `{exe_name} version`

- Ping:
  - `{exe_name} ping`

## Config

`config.example.json` is provided as an example. If you later need runtime config, copy it to `config.json` and extend the app to read it.
"""


def build_evidence(
    product_id: str,
    version: str,
    inputs: dict[str, Any],
    checks: dict[str, Any],
    toolchain: dict[str, Any],
    pyinstaller_cmd: list[str],
    pyinstaller_res: dict[str, Any],
    postcheck: dict[str, Any],
) -> str:
    def fence(s: str) -> str:
        return "```\n" + s.rstrip() + "\n```\n"

    md: list[str] = []
    md.append("# Evidence (EXE Pack v0)\n")
    md.append(f"- ts_utc: `{utc_ts()}`\n")
    md.append(f"- product_id: `{product_id}`\n")
    md.append(f"- version: `{version}`\n")

    md.append("\n## Inputs\n")
    md.append(fence(json.dumps(inputs, indent=2, ensure_ascii=False)))

    md.append("\n## Toolchain\n")
    md.append(fence(json.dumps(toolchain, indent=2, ensure_ascii=False)))

    md.append("\n## Preflight checks\n")
    md.append("### py_compile\n")
    md.append(fence(json.dumps(checks.get("py_compile"), indent=2, ensure_ascii=False)))
    md.append("### ruff\n")
    md.append(fence(json.dumps(checks.get("ruff"), indent=2, ensure_ascii=False)))
    md.append("### python smoke\n")
    md.append(fence(json.dumps(checks.get("smoke"), indent=2, ensure_ascii=False)))

    md.append("\n## Build (PyInstaller)\n")
    md.append("### command\n")
    md.append(fence(" ".join(pyinstaller_cmd)))
    md.append("### result\n")
    md.append(fence(json.dumps(pyinstaller_res, indent=2, ensure_ascii=False)))

    md.append("\n## Post-build check\n")
    md.append(fence(json.dumps(postcheck, indent=2, ensure_ascii=False)))

    return "".join(md)


def classify_exit_code(step: str, exc: Exception) -> int:
    """
    Project convention:
    - 1 = FAIL (expected/semantic failure)
    - 2 = INFRA (toolchain/unexpected)
    """
    # Missing tools / infra-ish
    msg = str(exc)
    infra_markers = [
        "PyInstaller is not available",
        "No module named",
        "ruff",
        "pip",
    ]
    if any(m in msg for m in infra_markers):
        return 2
    # Default: FAIL for product/workspace/config/entrypoint issues
    if isinstance(exc, (FileNotFoundError, ValueError, RuntimeError)):
        return 1
    return 2


def main_inner() -> Tuple[dict[str, Any], int]:
    ap = argparse.ArgumentParser(description="Build Windows portable EXE for a product (EXE Pack v0)")
    ap.add_argument("--repo", default=".", help="Repo root")
    ap.add_argument("--product-id", required=False)
    ap.add_argument("--out-dir", default=None, help="Override dist/<product_id>")
    ap.add_argument("--workspace", default=None, help="Build directly from a workspace directory")
    ap.add_argument("--entrypoint", default=None, help="Entrypoint relative to workspace (e.g., src/main.py)")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    inputs: dict[str, Any] = {}
    checks: dict[str, Any] = {}
    toolchain: dict[str, Any] = {}
    pyinstaller_cmd: list[str] = []
    pyinstaller_res: dict[str, Any] = {}
    postcheck: dict[str, Any] = {}

    # Resolve build inputs (Mode A / Mode B)
    if args.workspace is None:
        if not args.product_id:
            raise ValueError("provide --product-id or --workspace")
        product = load_product(repo, args.product_id)
        product_id = product.product_id
        version = product.version
        src_root = (repo / product.template_dir).resolve()
        entry_script = (src_root / product.entrypoint).resolve()
        exe_name = product.exe_name
        config_src = src_root / "config.example.json"

        inputs = {
            "mode": "product_id",
            "product_id": product_id,
            "template_root": str(src_root),
            "entrypoint": str(entry_script),
        }
    else:
        ws = Path(args.workspace).resolve()
        if not ws.exists():
            raise FileNotFoundError(f"workspace not found: {ws}")
        if not args.entrypoint:
            raise ValueError("--entrypoint is required when using --workspace")

        rel_ep = Path(args.entrypoint)
        entry_script = (ws / rel_ep).resolve()
        entry_script.relative_to(ws)  # sanity: must be inside workspace
        exe_name = "app.exe"
        product_id = args.product_id or "workspace_build"
        version = "0.0.0"
        config_src = ws / "config.example.json"
        if not config_src.exists():
            raise FileNotFoundError(f"config.example.json not found in workspace: {config_src}")
        src_root = ws

        inputs = {
            "mode": "workspace",
            "product_id": product_id,
            "workspace": str(ws),
            "entrypoint": str(entry_script),
        }

    if not entry_script.exists():
        raise FileNotFoundError(f"entrypoint not found: {entry_script}")

    out_dir = Path(args.out_dir).resolve() if args.out_dir else (repo / "dist" / product_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    exe_base = exe_name[:-4] if exe_name.lower().endswith(".exe") else exe_name
    exe_path = out_dir / exe_name

    # Preflight checks
    checks["py_compile"] = py_compile_tree(src_root)
    checks["ruff"] = run([sys.executable, "-m", "ruff", "check", str(src_root)])

    # Smoke (critical): ensure src-root importability
    # We run with:
    # - cwd = src_root
    # - env PYTHONPATH includes src_root
    env = os.environ.copy()
    pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(src_root) if not pp else (str(src_root) + os.pathsep + pp)

    smoke_help = run([sys.executable, str(entry_script), "--help"], cwd=src_root, env=env)
    checks["smoke"] = {"help": smoke_help, "cwd": str(src_root), "py_path": env["PYTHONPATH"]}

    # Write smoke evidence for fast debugging
    write_text(out_dir / "python_smoke_help.stdout.txt", smoke_help.get("stdout", ""))
    write_text(out_dir / "python_smoke_help.stderr.txt", smoke_help.get("stderr", ""))

    if not checks["py_compile"]["ok"]:
        raise RuntimeError("py_compile failed; see evidence")
    if smoke_help["rc"] != 0:
        raise RuntimeError("python smoke failed; see evidence")

    # Toolchain
    toolchain = {
        "python": sys.version.replace("\n", " "),
        "python_exe": sys.executable,
        "platform": platform.platform(),
        "pip": run([sys.executable, "-m", "pip", "--version"]),
        "ruff": run([sys.executable, "-m", "ruff", "--version"]),
        "pyinstaller": run([sys.executable, "-m", "PyInstaller", "--version"]),
    }
    if toolchain["pyinstaller"]["rc"] != 0:
        raise RuntimeError("PyInstaller is not available. Run scripts/bootstrap_tools_v1.ps1 first.")

    # Clean old exe if exists
    try:
        if exe_path.exists():
            exe_path.unlink()
    except Exception:
        pass

    # Build dirs
    build_root = repo / "dist" / "_pyi_build"
    build_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work_dir = build_root / f"{product_id}__{stamp}__work"
    spec_dir = build_root / f"{product_id}__{stamp}__spec"

    # PyInstaller paths:
    # - include src_root so imports like "src.*" work
    # - also include entry_script.parent for direct module imports
    pyinstaller_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        exe_base,
        "--distpath",
        str(out_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--paths",
        str(src_root),
        "--paths",
        str(entry_script.parent),
        str(entry_script),
    ]

    pyinstaller_res = run(pyinstaller_cmd, cwd=src_root, env=env)
    if pyinstaller_res["rc"] != 0:
        # also persist pyinstaller logs quickly
        write_text(out_dir / "pyinstaller.stdout.txt", pyinstaller_res.get("stdout", ""))
        write_text(out_dir / "pyinstaller.stderr.txt", pyinstaller_res.get("stderr", ""))
        raise RuntimeError("PyInstaller build failed; see evidence")

    candidate = out_dir / (exe_base + ".exe")
    if candidate.exists() and exe_path != candidate:
        try:
            if exe_path.exists():
                exe_path.unlink()
        except Exception:
            pass
        candidate.replace(exe_path)

    if not exe_path.exists():
        raise FileNotFoundError(f"app.exe not found after build: {exe_path}")

    # Copy config
    config_dst = out_dir / "config.example.json"
    shutil.copyfile(str(config_src), str(config_dst))

    # Postcheck
    postcheck = run([str(exe_path), "--help"], cwd=out_dir)
    if postcheck["rc"] != 0:
        raise RuntimeError("built app.exe --help failed")

    # Artifacts
    runbook_path = out_dir / "runbook.md"
    evidence_path = out_dir / "evidence.md"
    hashes_path = out_dir / "hashes.json"

    write_text(runbook_path, build_runbook(exe_name))

    evidence_md = build_evidence(
        product_id=product_id,
        version=version,
        inputs=inputs,
        checks=checks,
        toolchain=toolchain,
        pyinstaller_cmd=pyinstaller_cmd,
        pyinstaller_res=pyinstaller_res,
        postcheck=postcheck,
    )
    write_text(evidence_path, evidence_md)

    hashes = {
        "schema": "hashes_v0",
        "product_id": product_id,
        "ts_utc": utc_ts(),
        "files": {
            exe_name: sha256_file(exe_path),
            "config.example.json": sha256_file(config_dst),
            "runbook.md": sha256_file(runbook_path),
            "evidence.md": sha256_file(evidence_path),
        },
    }
    write_text(hashes_path, json.dumps(hashes, indent=2, ensure_ascii=False) + "\n")

    out = {
        "schema": SCHEMA,
        "ts_utc": utc_ts(),
        "product_id": product_id,
        "version": version,
        "out_dir": str(out_dir),
        "artifacts": {
            "app_exe": str(exe_path),
            "config_example": str(config_dst),
            "runbook": str(runbook_path),
            "evidence": str(evidence_path),
            "hashes": str(hashes_path),
            "smoke_help_stdout": str(out_dir / "python_smoke_help.stdout.txt"),
            "smoke_help_stderr": str(out_dir / "python_smoke_help.stderr.txt"),
        },
        "inputs": inputs,
        "checks": checks,
    }
    return out, 0


def main() -> int:
    try:
        payload, code = main_inner()
        return emit_and_exit(payload, code)
    except Exception as e:  # noqa: BLE001
        # Best-effort minimal structured error
        code = classify_exit_code("unhandled", e)
        payload = {
            "schema": SCHEMA,
            "ts_utc": utc_ts(),
            "error": {
                "kind": e.__class__.__name__,
                "message": str(e),
            },
        }
        return emit_and_exit(payload, code)


if __name__ == "__main__":
    raise SystemExit(main())


