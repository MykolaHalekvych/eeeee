import argparse
import html
import json
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


def _read_json_bom(path: Path):
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8-sig"))


def _default_registry_path() -> Path:
    # 1) рядом с exe (dist\...\app.exe -> dist\...\configs\...)
    base = Path(sys.argv[0]).resolve().parent
    cand = base / "configs" / "ecosystem_registry_v0.json"
    if cand.exists():
        return cand

    # 2) запуск из исходников: templates\...\src\app.py -> templates\...\configs\...
    try:
        here = Path(__file__).resolve()
        cand2 = here.parent.parent / "configs" / "ecosystem_registry_v0.json"
        if cand2.exists():
            return cand2
    except Exception:
        pass

    return cand


def _load_registry(registry_path: Path):
    try:
        reg = _read_json_bom(registry_path)
        return {"ok": True, "registry": reg, "error": None}
    except FileNotFoundError:
        return {"ok": False, "registry": None, "error": {"kind": "INFRA", "message": f"registry not found: {registry_path}"}}
    except Exception as e:
        return {"ok": False, "registry": None, "error": {"kind": "PARSE_ERROR", "message": f"registry parse error: {e}"}}


def _systems_from_registry(reg):
    systems = reg.get("systems", []) if isinstance(reg, dict) else []
    out = []
    for s in systems:
        if not isinstance(s, dict):
            continue
        out.append({
            "system_id": s.get("system_id"),
            "display_name": s.get("display_name") or s.get("system_id"),
            "repo_path": s.get("repo_path"),
            "runs_root": s.get("runs_root"),
            "releases_root": s.get("releases_root"),
            "proof_packs_root": s.get("proof_packs_root"),
            "version_tag": s.get("version_tag"),
        })
    return out


def _html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 20px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; vertical-align: top; }}
    th {{ background: #f3f3f3; text-align: left; }}
    .small {{ color: #555; font-size: 12px; }}
    code {{ background: #f6f6f6; padding: 2px 4px; border-radius: 4px; }}
  </style>
</head>
<body>
{body}
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path or "/"

        if path == "/health":
            return self._json(200, {"ok": True})

        if path in ("/", "/systems"):
            return self._systems_html()

        if path == "/api/registry":
            return self._registry_json()

        if path == "/api/systems":
            return self._systems_json()

        return self._text(404, "Not Found")

    def log_message(self, fmt, *args):
        # quiet by default
        return

    def _load(self):
        return _load_registry(self.server.registry_path)

    def _registry_json(self):
        res = self._load()
        if not res["ok"]:
            return self._json(503, res)
        return self._json(200, {"ok": True, "registry_path": str(self.server.registry_path), "registry": res["registry"]})

    def _systems_json(self):
        res = self._load()
        if not res["ok"]:
            return self._json(503, res)
        systems = _systems_from_registry(res["registry"])
        return self._json(200, {"ok": True, "registry_path": str(self.server.registry_path), "systems": systems})

    def _systems_html(self):
        res = self._load()
        if not res["ok"]:
            msg = html.escape(res["error"]["message"])
            body = (
                "<h1>Systems</h1>"
                "<p><b>Status:</b> INFRA</p>"
                f"<p class='small'><code>{msg}</code></p>"
                "<p class='small'>API: <a href='/api/systems'>/api/systems</a> | <a href='/api/registry'>/api/registry</a></p>"
            )
            return self._html(200, _html_page("Ecosystem Console v0", body))

        systems = _systems_from_registry(res["registry"])
        rows = []
        for s in systems:
            rows.append(
                "<tr>"
                f"<td><b>{html.escape(str(s.get('system_id') or ''))}</b></td>"
                f"<td>{html.escape(str(s.get('display_name') or ''))}</td>"
                f"<td class='small'>{html.escape(str(s.get('repo_path') or ''))}</td>"
                f"<td class='small'>{html.escape(str(s.get('runs_root') or ''))}</td>"
                f"<td class='small'>{html.escape(str(s.get('releases_root') or ''))}</td>"
                "</tr>"
            )

        table = (
            "<table><thead><tr>"
            "<th>system_id</th><th>display</th><th>repo_path</th><th>runs_root</th><th>releases_root</th>"
            "</tr></thead><tbody>"
            + "".join(rows) +
            "</tbody></table>"
        )

        rp = html.escape(str(self.server.registry_path))
        body = (
            "<h1>Systems</h1>"
            f"<p class='small'>registry: <code>{rp}</code></p>"
            + table +
            "<p class='small'>API: <a href='/api/systems'>/api/systems</a> | <a href='/api/registry'>/api/registry</a></p>"
        )
        return self._html(200, _html_page("Ecosystem Console v0", body))

    def _json(self, code: int, obj):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, code: int, text: str):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, code: int, page: str):
        data = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(
        prog="ecosystem_console_v0",
        description="Ecosystem Console v0 (read-only)."
    )
    p.add_argument("--registry", default=None, help="Path to configs/ecosystem_registry_v0.json")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--print-registry", action="store_true", help="Print resolved registry path and exit.")
    args = p.parse_args(argv)

    reg = Path(args.registry) if args.registry else _default_registry_path()
    if args.print_registry:
        print(str(reg))
        return 0

    httpd = HTTPServer((args.host, args.port), Handler)
    httpd.registry_path = reg
    print(f"Serving on http://{args.host}:{args.port}/ (registry={reg})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
