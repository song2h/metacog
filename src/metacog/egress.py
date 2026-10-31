"""Egress map: a local HTTP(S) proxy that records every host an agent process connects to.

Hooks only see the agent's tool calls. Traffic from the agent binary itself, its model
provider, or child processes that make their own requests is invisible to them, so `wrap`
starts this proxy and points HTTPS_PROXY/HTTP_PROXY at it. TLS is tunneled, never decrypted:
only host and port are recorded. Programs that ignore proxy env vars are not seen.
"""

import json
import socket
import socketserver
import threading
import time

from metacog import guard, store


def log_path(run_id: str):
    return store.HOME / "egress" / f"{run_id}.jsonl"


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        s = self.request
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                return
            head += chunk
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        try:
            method, target, _ = line.split(" ", 2)
        except ValueError:
            return
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
        else:
            rest = target.split("://", 1)[-1].split("/", 1)[0]
            host, _, port = rest.partition(":")
            port = port or "80"
        host = host.strip("[]").lower()
        cfg = self.server.cfg
        blocked = guard.allowed(host, cfg.get("block_hosts", []))
        rec = {"ts": time.time(), "host": host, "port": int(port or 443), "method": method,
               "allowed": guard.allowed(host, cfg["allow_hosts"]), "blocked": blocked}
        with self.server.lock, log_path(self.server.run_id).open("a") as f:
            f.write(json.dumps(rec) + "\n")
        if blocked:
            s.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            up = socket.create_connection((host, int(port or 443)), timeout=15)
            up.settimeout(None)
        except OSError:
            s.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        if method == "CONNECT":
            s.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            up.sendall(head)
        t = threading.Thread(target=_pipe, args=(up, s), daemon=True)
        t.start()
        _pipe(s, up)
        t.join()
        up.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start(run_id: str) -> int:
    """Start the proxy on a free localhost port in a background thread; return the port."""
    log_path(run_id).parent.mkdir(parents=True, exist_ok=True)
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.run_id, srv.cfg, srv.lock = run_id, store.config(), threading.Lock()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def summary(run_id: str) -> list[dict]:
    p = log_path(run_id)
    hosts: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            r = json.loads(line)
            h = hosts.setdefault(r["host"], {"host": r["host"], "count": 0, "allowed": r["allowed"], "blocked": r["blocked"]})
            h["count"] += 1
    return sorted(hosts.values(), key=lambda h: (h["allowed"], -h["count"]))
