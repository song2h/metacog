"""Local dashboard: stdlib HTTP server over the JSONL session logs. Binds to 127.0.0.1 only."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import unquote

from metacog import egress, store


def session_summary(path) -> dict:
    recs = store.read(path.stem)
    first = recs[0] if recs else {}
    verdicts = [r for r in recs if r.get("event") == "Verdict"]
    return {
        "id": path.stem, "agent": first.get("agent"), "cwd": first.get("cwd"),
        "start": first.get("ts"), "last": recs[-1].get("ts") if recs else None,
        "turns": len(verdicts),
        "guarded": sum(r.get("event") == "Guard" and r.get("decision") in ("deny", "ask") for r in recs),
        "false_claims": sum(c["verdict"] == "false" for v in verdicts for c in v.get("claims", [])) +
                        sum(f["severity"] == "false" for v in verdicts for f in v.get("findings", [])),
        "trust": next((v["trust"] for v in reversed(verdicts) if v.get("trust") is not None), None),
    }


def egress_runs(limit: int = 10) -> list[dict]:
    d = store.HOME / "egress"
    if not d.exists():
        return []
    runs = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [{"run": p.stem, "hosts": egress.summary(p.stem)} for p in runs]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, ctype: str = "application/json", code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # A page on another origin can rebind its DNS name to 127.0.0.1; only answer requests addressed to us.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            return self._send(b"{}", code=403)
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(files("metacog").joinpath("dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/sessions":
            return self._send(json.dumps([session_summary(p) for p in store.sessions()[:50]]).encode())
        if path.startswith("/api/session/"):
            return self._send(json.dumps(store.read(unquote(path.rsplit("/", 1)[1]))).encode())
        if path == "/api/egress":
            return self._send(json.dumps(egress_runs()).encode())
        self._send(b"{}", code=404)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # closed browser tabs etc. must not print over the agent's terminal


def _bind(port: int) -> ThreadingHTTPServer:
    return _Server(("127.0.0.1", port), _Handler)


def serve(port: int) -> None:
    srv = _bind(port)
    print(f"metacog dashboard on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def start_background(port: int) -> str:
    """Start the dashboard in a daemon thread, or reuse one already listening on the port."""
    url = f"http://127.0.0.1:{port}"
    try:
        srv = _bind(port)
    except OSError:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return url + " (already running)"
        raise
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return url
