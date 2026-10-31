"""Which country a destination belongs to, so traffic to chosen countries can be blocked and logged.

Two signals, because neither is enough alone:
- IP ranges: APNIC's public delegation file says which address blocks are allocated to which
  country. Downloaded on demand (`metacog init`, first `wrap`), never inside a hook.
  Allocation is not location: an allocated block can be used abroad, and a Chinese service on a
  foreign CDN or cloud region has foreign IPs.
- Domain names: services tied to a country by public reporting (see DOMAINS), plus country TLDs.

Policy: config "block_countries" (default ["CN"]); env METACOG_BLOCK_COUNTRIES overrides it
("" turns blocking off and leaves detection on, "CN,HK" adds Hong Kong).
"""

import bisect
import ipaddress
import os
import re
import urllib.request

from metacog import store

APNIC_URL = "https://ftp.apnic.net/stats/apnic/delegated-apnic-latest"
TLDS = {"CN": (".cn",), "HK": (".hk",), "MO": (".mo",)}
# Hostname suffix -> country, each tied to a public source. Matched on whole labels.
DOMAINS: dict[str, str] = {}
IP_LITERAL = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|\[[0-9a-fA-F:]+\]")

_ranges: dict[int, tuple[list[int], list[int], list[str]]] | None = None


def blocked_countries(cfg: dict) -> set[str]:
    env = os.environ.get("METACOG_BLOCK_COUNTRIES")
    raw = env.split(",") if env is not None else cfg.get("block_countries", ["CN"])
    return {c.strip().upper() for c in raw if c.strip()}


def watched_countries(cfg: dict) -> set[str]:
    return blocked_countries(cfg) | {c.upper() for c in cfg.get("watch_countries", ["CN"])}


def data_path():
    return store.HOME / "geo" / "apnic.txt"


def update(url: str = APNIC_URL, timeout: float = 60) -> int:
    """Download the APNIC delegation file and keep 'CC version start size' lines. Returns the range count."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        text = r.read().decode("ascii", "replace")
    rows = []
    for line in text.splitlines():
        p = line.split("|")
        if len(p) >= 7 and p[2] in ("ipv4", "ipv6") and p[6] in ("allocated", "assigned") and len(p[1]) == 2:
            rows.append(f"{p[1]} {p[2][-1]} {p[3]} {p[4]}")
    if not rows:
        raise RuntimeError("APNIC file had no ranges")
    path = data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(rows) + "\n")
    tmp.replace(path)
    global _ranges
    _ranges = None
    return len(rows)


def _load() -> dict[int, tuple[list[int], list[int], list[str]]]:
    global _ranges
    if _ranges is None:
        spans = {4: [], 6: []}
        if data_path().exists():
            for line in data_path().read_text().splitlines():
                cc, ver, start, size = line.split()
                first = int(ipaddress.ip_address(start))
                # APNIC gives an address count for IPv4 and a prefix length for IPv6.
                last = first + int(size) - 1 if ver == "4" else first + 2 ** (128 - int(size)) - 1
                spans[int(ver)].append((first, last, cc))
        _ranges = {}
        for ver, rows in spans.items():
            rows.sort()
            _ranges[ver] = ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows])
    return _ranges


def have_data() -> bool:
    return data_path().exists()


def ip_country(ip: str) -> str | None:
    try:
        addr = ipaddress.ip_address(ip.strip("[]"))
    except ValueError:
        return None
    starts, ends, ccs = _load()[addr.version]
    i = bisect.bisect_right(starts, int(addr)) - 1
    return ccs[i] if i >= 0 and int(addr) <= ends[i] else None


def host_country(host: str) -> tuple[str, str] | None:
    """(country, why) from the name alone: an IP literal, a listed domain or a country TLD."""
    host = host.lower().rstrip(".").strip("[]")
    if (cc := ip_country(host)):
        return cc, f"{host} is in an APNIC range allocated to {cc}"
    for suffix, cc in DOMAINS.items():
        if host == suffix or host.endswith("." + suffix):
            return cc, f"{suffix} is a {cc} service (domain list)"
    for cc, tlds in TLDS.items():
        if host.endswith(tlds):
            return cc, f"{cc} country domain"
    return None


def classify(host: str, ips: list[str]) -> tuple[str, str] | None:
    """(country, why) for a destination, also checking the addresses it resolved to."""
    if (hit := host_country(host)):
        return hit
    for ip in ips:
        if (cc := ip_country(ip)):
            return cc, f"{host} resolves to {ip}, in an APNIC range allocated to {cc}"
    return None
