"""Ketchup web page: three chats (Mayo, the agents, Frankie) on one page.

    python web/server.py            # then open http://localhost:8000
    python web/server.py --port 9000

Reads keys from .env in the repo root (see .env.example). Stdlib only; the
page polls GET /api/session/<id>?since=<n> about once a second.

API
  POST /api/start                {"phrase": "...", "dry_run": true}  -> {"session_id"}
  GET  /api/session/<id>?since=n -> new messages, thinking, waiting_for, state
  POST /api/session/<id>/say     {"person": "mayo"|"frankie", "text": "..."}
"""
import argparse
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent


def load_env(path=ROOT / ".env"):
    """KEY=value lines; values already in the environment win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if v:
            os.environ.setdefault(k.strip(), v)


load_env()
sys.path.insert(0, str(ROOT / "brain"))
from web_flow import WebSession  # noqa: E402

SESSIONS = {}


def missing_keys():
    out = []
    if not os.environ.get("BASETEN_API_KEY") and not os.environ.get("META_API_KEY"):
        out.append("BASETEN_API_KEY")
    if not os.environ.get("GOOGLE_SA_JSON") and not os.environ.get("GOOGLE_SA_FILE"):
        out.append("GOOGLE_SA_FILE (or GOOGLE_SA_JSON)")
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet: polling would flood the terminal
        if "/api/session/" in self.path and self.command == "GET":
            return
        sys.stderr.write(f"{self.command} {self.path}\n")

    def send(self, code, body, kind="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        parts = u.path.strip("/").split("/")
        if parts[:2] == ["api", "session"] and len(parts) == 3:
            s = SESSIONS.get(parts[2])
            if not s:
                return self.send(404, {"error": "no such session"})
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            return self.send(200, s.public(since))
        if u.path == "/api/health":
            return self.send(200, {"ok": not missing_keys(), "missing": missing_keys()})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        try:
            b = self.body()
        except json.JSONDecodeError:
            return self.send(400, {"error": "bad JSON"})
        if parts == ["api", "start"]:
            if missing_keys():
                return self.send(400, {"error": "missing in .env: " + ", ".join(missing_keys())})
            sid = uuid.uuid4().hex[:8]
            SESSIONS[sid] = WebSession(sid, (b.get("phrase") or "let's catch up this week").strip(),
                                       dry_run=bool(b.get("dry_run", True)))
            return self.send(200, {"session_id": sid})
        if len(parts) == 4 and parts[:2] == ["api", "session"] and parts[3] == "say":
            s = SESSIONS.get(parts[2])
            if not s:
                return self.send(404, {"error": "no such session"})
            r = s.answer(b.get("person", ""), b.get("text", ""))
            return self.send(200 if r["ok"] else 409, r)
        self.send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    if missing_keys():
        print("Warning: missing in .env:", ", ".join(missing_keys()))
    print(f"Ketchup web page: http://localhost:{a.port}  (Ctrl+C to stop)")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
