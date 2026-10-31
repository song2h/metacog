"""Catch hallucinated dependencies before they are installed.

Code models name packages that do not exist (on average at least 5.2% for commercial and 21.7% for
open-source models in Spracklen et al., "We Have a Package for You!", arXiv 2406.10279). Anyone can
register such a name later and wait for agents to install it ("slopsquatting"). So before
`pip install` / `npm install` runs, metacog asks the public registry: a missing package is denied,
a package first published recently needs your approval.

Skipped for private indexes (--index-url, --registry, ...) and when "check_packages" is false.
Registry errors never block: the check is advice, the install still has its own failure modes.
"""

import json
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request

INSTALL = re.compile(r"\b(?:(?:uv\s+)?pip3?|python3?\s+-m\s+pip)\s+install\b|\buv\s+add\b|\bpoetry\s+add\b|"
                     r"\b(?:npm|pnpm|bun)\s+(?:install|i|add)\b|\byarn\s+add\b")
CUSTOM_INDEX = re.compile(r"(?:^|\s)(-i|--index-url|--extra-index-url|--registry|--find-links|-f)\b")
SEPARATOR = re.compile(r"&&|\|\||[;|\n]")
NAME = re.compile(r"^(@[a-z0-9][\w.-]*/)?[A-Za-z0-9][\w.-]*$")


def _ecosystem(part: str) -> str:
    return "npm" if re.search(r"\b(npm|pnpm|yarn|bun)\b", part) else "pypi"


def requested(cmd: str) -> list[tuple[str, str]]:
    """(ecosystem, name) for every package an install command names on the command line."""
    out = []
    for part in SEPARATOR.split(cmd):
        m = INSTALL.search(part)
        if not m or CUSTOM_INDEX.search(part):
            continue
        try:
            words = shlex.split(part[m.end():])
        except ValueError:
            continue
        eco, skip = _ecosystem(part), False
        for w in words:
            if skip:
                skip = False
                continue
            if w in ("-r", "--requirement", "-c", "--constraint", "-e", "--editable", "--target", "-t", "--prefix"):
                skip = True
                continue
            if w.startswith("-") or "/" in w and not w.startswith("@") or "://" in w or w in (".", ".."):
                continue
            name = re.split(r"[=<>!~;\[ ]", w, maxsplit=1)[0]
            if eco == "npm" and name.count("@") > (1 if name.startswith("@") else 0):
                name = name.rsplit("@", 1)[0]  # pkg@1.2 or @scope/pkg@1.2
            if NAME.match(name):
                out.append((eco, name))
    return out[:5]


def lookup(eco: str, name: str, timeout: float = 3) -> tuple[bool, float | None] | None:
    """(exists, first publish time) from the public registry, or None if the registry could not be asked."""
    url = (f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json" if eco == "pypi"
           else f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return (False, None) if e.code == 404 else None
    except (OSError, ValueError):
        return None
    if eco == "pypi":
        times = [f["upload_time_iso_8601"] for files in data.get("releases", {}).values() for f in files if f.get("upload_time_iso_8601")]
    else:
        times = [data.get("time", {}).get("created")] if data.get("time", {}).get("created") else []
    first = min(times) if times else None
    return True, (time.mktime(time.strptime(first[:19], "%Y-%m-%dT%H:%M:%S")) if first else None)


def check(cmd: str, cfg: dict) -> dict | None:
    if not cfg.get("check_packages", True) or not INSTALL.search(cmd):
        return None
    min_days = cfg.get("new_package_days", 30)
    for eco, name in requested(cmd):
        found = lookup(eco, name)
        if found is None:
            continue
        exists, first = found
        registry = "PyPI" if eco == "pypi" else "npm"
        if not exists:
            return {"decision": "deny", "rule": "package-missing", "package": name,
                    "reason": f"`{name}` does not exist on {registry}: likely a hallucinated name, and anyone could register it. "
                              f"Check the real package name"}
        if first and (age := (time.time() - first) / 86400) < min_days:
            return {"decision": "ask", "rule": "package-new", "package": name,
                    "reason": f"`{name}` was first published on {registry} {age:.0f} days ago; new names are how hallucinated "
                              f"dependencies get squatted"}
    return None
