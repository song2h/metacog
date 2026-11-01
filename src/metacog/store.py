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
    """Defaults plus ~/.metacog/config.json. A broken file must not switch the guards off: it is logged, the defaults apply
    and "config_error" says why (shown by `metacog doctor`)."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        user = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
        if not isinstance(user, dict):
            raise ValueError("expected a JSON object")
    except (OSError, ValueError) as e:
        cfg["config_error"] = f"{CONFIG_FILE}: {e}"
        log_error(f"config: {cfg['config_error']}; using the defaults")
        return cfg
    for k, v in user.items():
        # List settings add to the defaults: allow_hosts ["api.corp.com"] must not drop localhost and the package registries.
        base = DEFAULT_CONFIG.get(k)
        cfg[k] = base + [x for x in v if x not in base] if isinstance(base, list) and isinstance(v, list) else v
    return cfg


def log_error(text: str) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with (HOME / "errors.log").open("a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(f"{time.ctime()} {text}\n")
    except OSError:
        pass


def _safe(sid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sid or "unknown")


def session_path(sid: str) -> Path:
    return SESSIONS / f"{_safe(sid)}.jsonl"


def _private_dir(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)  # logs hold prompts, commands and outputs


def append(sid: str, rec: dict) -> dict:
    rec.setdefault("ts", time.time())
    _private_dir(SESSIONS)
    # One write() of an ASCII-only line: no locale or lone-surrogate surprises, and concurrent hooks don't interleave.
    line = (json.dumps(rec, ensure_ascii=True) + "\n").encode()
    fd = os.open(session_path(sid), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return rec


def read(sid: str) -> list[dict]:
    p = session_path(sid)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
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
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and not k.startswith("#"):
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))
