"""Egress map: a local HTTP(S) proxy that records every host an agent process connects to.

Hooks only see the agent's tool calls. Traffic from the agent binary itself, its model
provider, or child processes that make their own requests is invisible to them, so `wrap`
starts this proxy and points HTTPS_PROXY/HTTP_PROXY at it. TLS is tunneled, never decrypted:
only host and port are recorded. Programs that ignore proxy env vars are not seen.
"""

import json
import re
import socket
import socketserver
import threading
import time

from metacog import geo, guard, store

IP_HOST = re.compile(r"\d{1,3}(\.\d{1,3}){3}|[0-9a-f]*:[0-9a-f:.]*")


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
        s.settimeout(30)
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
        authority = target if method == "CONNECT" else target.split("://", 1)[-1].split("/", 1)[0]
        host, port = split_authority(authority, 443 if method == "CONNECT" else 80)
        if not host or port is None:
            s.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            return
        cfg = self.server.cfg
        allowed = guard.allowed(host, cfg["allow_hosts"])
        blocked = guard.allowed(host, cfg.get("block_hosts", [])) or (self.server.enforce and not allowed)
        try:
            ips = list(dict.fromkeys(a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except OSError:
            ips = []
        rec = {"ts": time.time(), "host": host, "port": port, "method": method, "allowed": allowed, "blocked": blocked}
        if (hit := geo.classify(host, ips)) and hit[0] in geo.watched_countries(cfg):
            rec.update(country=hit[0], why=hit[1])
            if hit[0] in self.server.block_countries and not allowed:
                rec["blocked"] = blocked = True
        if IP_HOST.fullmatch(host):
            rec["direct_ip"] = True
        with self.server.lock, log_path(self.server.run_id).open("a") as f:
            f.write(json.dumps(rec) + "\n")
        if blocked:
            s.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        up = None
        for ip in ips:  # only addresses that were checked, so DNS cannot point elsewhere after the check
            try:
                up = socket.create_connection((ip, port), timeout=15)
                break
            except OSError:
                continue
        if up is None:
            s.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        up.settimeout(None)
        s.settimeout(None)
        if method == "CONNECT":
            s.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            # One request per connection: clients pool proxy connections across hosts, so a kept-alive
            # tunnel to the first origin would carry later requests meant for other hosts.
            first, _, rest = head.partition(b"\r\n")
            kept = [h for h in rest.split(b"\r\n") if not h.lower().startswith((b"connection:", b"proxy-connection:", b"keep-alive:"))]
            up.sendall(first + b"\r\nConnection: close\r\n" + b"\r\n".join(kept))
        t = threading.Thread(target=_pipe, args=(up, s), daemon=True)
        t.start()
        _pipe(s, up)
        t.join()
        up.close()


def split_authority(authority: str, default_port: int) -> tuple[str, int | None]:
    """'host:port', '[v6]:port' or 'host' -> (host, port); port None when it is not a valid number."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif authority.count(":") == 1:
        host, _, port = authority.partition(":")
    else:
        host, port = authority, ""
    if not port:
        return host.lower(), default_port
    return host.lower(), int(port) if port.isdigit() and 0 < int(port) < 65536 else None


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # resets and timeouts are normal for a proxy; never print over the agent's terminal


def start(run_id: str, enforce: bool = False) -> int:
    """Start the proxy on a free localhost port in a background thread; return the port.

    enforce=True refuses every host that is not on the allow list (403), which also stops
    uploads hidden inside scripts that the command guard never sees.
    """
    log_path(run_id).parent.mkdir(parents=True, exist_ok=True)
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.run_id, srv.cfg, srv.lock, srv.enforce = run_id, store.config(), threading.Lock(), enforce
    srv.block_countries = geo.blocked_countries(srv.cfg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def summary(run_id: str) -> list[dict]:
    p = log_path(run_id)
    hosts: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            r = json.loads(line)
            h = hosts.setdefault(r["host"], {"host": r["host"], "count": 0, "allowed": r["allowed"], "blocked": r["blocked"],
                                             "country": r.get("country"), "why": r.get("why")})
            h["count"] += 1
    return sorted(hosts.values(), key=lambda h: (h["allowed"], -h["count"]))
