"""Session event log: one JSONL file per agent session under ~/.metacog."""

import json
import os
import re
import time
from pathlib import Path

HOME = Path(os.environ.get("METACOG_HOME", Path.home() / ".metacog"))
SESSIONS = HOME / "sessions"
CONFIG_FILE = HOME / "config.json"
ENV_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "metacog" / ".env"

DEFAULT_CONFIG = {
    "model": "muse-spark-1.3",
    "allow_hosts": [
        "github.com", "api.github.com", "raw.githubusercontent.com", "objects.githubusercontent.com",
        "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "proxy.golang.org", "crates.io",
        "api.anthropic.com", "api.meta.ai", "api.openai.com", "localhost", "127.0.0.1",
    ],
    "block_hosts": [],
    "max_blocks_per_turn": 2,
}


def config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    return cfg


def _safe(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "unknown")


def session_path(sid: str) -> Path:
    return SESSIONS / f"{_safe(sid)}.jsonl"


def append(sid: str, rec: dict) -> dict:
    rec.setdefault("ts", time.time())
    SESSIONS.mkdir(parents=True, exist_ok=True)
    with session_path(sid).open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def read(sid: str) -> list[dict]:
    p = session_path(sid)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a half-written line from a concurrent hook
    return out


def sessions() -> list[Path]:
    if not SESSIONS.exists():
        return []
    return sorted(SESSIONS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def load_env() -> None:
    """Read KEY=VALUE lines from ~/.config/metacog/.env without overriding the real environment."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and not k.startswith("#"):
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))
