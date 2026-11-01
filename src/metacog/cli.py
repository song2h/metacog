"""metacog command line."""

import argparse
import getpass
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from metacog import __version__, egress, store

AGENTS = ("claude", "muse", "codex")
EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop")
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
MUSE_SETTINGS = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "muse" / "settings.json"
CODEX_HOOKS = Path.home() / ".codex" / "hooks.json"
PLUGIN_SOURCE = "song2h/metacog"


def hook_command(agent: str, portable: bool = False, headless: bool = False) -> str:
    if portable:
        return f"metacog hook {agent}"
    # wrap pins the interpreter and the log location: Muse runs hooks with a cleared environment.
    # Headless Muse waits forever on an `ask`, so wrap tells the hook there is nobody to approve.
    return (f"METACOG_HOME={shlex.quote(str(store.HOME))} {'METACOG_HEADLESS=1 ' if headless else ''}"
            f"{shlex.quote(sys.executable)} -m metacog hook {agent}")


def hooks_config(agent: str, portable: bool = False, headless: bool = False) -> dict:
    cfg = {}
    for ev in EVENTS:
        entry = {"hooks": [{"type": "command", "command": hook_command(agent, portable, headless), "timeout": 180 if ev == "Stop" else 15}]}
        if ev in TOOL_EVENTS and agent != "claude":
            entry["matcher"] = ".*"
        cfg[ev] = [entry]
    return cfg


def _is_ours(entry: dict) -> bool:
    return any("metacog hook" in h.get("command", "") for h in entry.get("hooks", []))


def _merge_hooks(existing: dict, ours: dict) -> dict:
    merged = {ev: [e for e in entries if not _is_ours(e)] for ev, entries in (existing or {}).items()}
    for ev, entries in ours.items():
        merged.setdefault(ev, []).extend(entries)
    return {ev: e for ev, e in merged.items() if e}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".metacog.bak"))
    path.write_text(json.dumps(data, indent=2) + "\n")


# ── commands ────────────────────────────────────────────────────────────────

def cmd_hook(a):
    from metacog import hook
    hook.main(a.agent)


def cmd_init(a):
    from metacog import llm
    key = llm.api_key()
    if not key:
        key = getpass.getpass("Meta Model API key (input hidden): ").strip()
        if not key:
            sys.exit("no key entered; nothing saved (get one at https://dev.meta.ai)")
        store.ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(store.ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"MODEL_API_KEY={key}\n")
        os.environ["MODEL_API_KEY"] = key
        print(f"saved to {store.ENV_FILE} (mode 600)")
    _ensure_geo(force=True)
    cmd_doctor(a)


def _ensure_geo(force: bool = False) -> None:
    """Fetch the APNIC ranges when country blocking or watching needs them. Never fatal."""
    from metacog import geo
    if geo.have_data() and not force or not geo.watched_countries(store.config()):
        return
    try:
        print(f"metacog: downloading IP allocation data from APNIC ({geo.APNIC_URL})...", file=sys.stderr)
        print(f"metacog: {geo.update()} IP ranges saved to {geo.data_path()}", file=sys.stderr)
    except Exception as e:  # domain rules still work without it
        print(f"metacog: could not fetch IP ranges ({e}); country checks use domain names only", file=sys.stderr)


def cmd_doctor(a):
    from metacog import llm
    ok = True
    cfg = store.config()
    print(f"metacog {__version__}  home={store.HOME}")
    if cfg.get("config_error"):
        ok = False
        print(f"  config     INVALID, using the defaults until it is fixed: {cfg['config_error']}")
    if llm.api_key():
        t0 = time.time()
        try:
            out, _ = llm.chat_json(cfg["model"], "Reply with JSON.", "Say ok.",
                                   {"title": "ping", "type": "object", "properties": {"ok": {"type": "boolean"}},
                                    "required": ["ok"], "additionalProperties": False})
            print(f"  verifier   {cfg['model']} reachable ({time.time() - t0:.1f}s)")
        except Exception as e:
            ok = False
            print(f"  verifier   FAILED: {e}")
    else:
        ok = False
        print("  verifier   no API key; run `metacog init` (deterministic checks still work)")
    for agent, binary in (("claude", "claude"), ("muse", "muse"), ("codex", "codex")):
        path = shutil.which(binary)
        print(f"  {agent:<10} {'found ' + path if path else 'not on PATH'}")
    from metacog import geo
    blocked, watched = geo.blocked_countries(cfg), geo.watched_countries(cfg)
    data = f"IP ranges from {time.strftime('%Y-%m-%d', time.localtime(geo.data_path().stat().st_mtime))}" if geo.have_data() else "no IP range data (run `metacog init`)"
    print(f"  countries  block {','.join(sorted(blocked)) or 'none'}, watch {','.join(sorted(watched)) or 'none'}; {data}")
    print(f"  muse hooks {'installed' if _muse_installed() else 'not installed'} ({MUSE_SETTINGS})")
    print(f"  codex hooks {'installed' if CODEX_HOOKS.exists() and 'metacog hook' in CODEX_HOOKS.read_text() else 'not installed'}")
    sys.exit(0 if ok else 1)


def _muse_installed() -> bool:
    return MUSE_SETTINGS.exists() and "metacog hook" in MUSE_SETTINGS.read_text()


def cmd_install(a):
    if a.agent == "claude":
        source = a.source or PLUGIN_SOURCE
        subprocess.run(["claude", "plugin", "marketplace", "add", source], check=True)
        subprocess.run(["claude", "plugin", "install", "metacog@metacog", "--scope", "user"], check=True)
    elif a.agent == "muse":
        data = json.loads(MUSE_SETTINGS.read_text()) if MUSE_SETTINGS.exists() else {"schema_version": 1}
        data["hooks"] = _merge_hooks(data.get("hooks"), hooks_config("muse", portable=True))
        _write_json(MUSE_SETTINGS, data)
        print(f"installed metacog hooks into {MUSE_SETTINGS}")
    else:
        data = json.loads(CODEX_HOOKS.read_text()) if CODEX_HOOKS.exists() else {}
        data["hooks"] = _merge_hooks(data.get("hooks"), hooks_config("codex", portable=True))
        _write_json(CODEX_HOOKS, data)
        print(f"installed metacog hooks into {CODEX_HOOKS} (review them once with /hooks inside Codex)")


def cmd_uninstall(a):
    if a.agent == "claude":
        subprocess.run(["claude", "plugin", "uninstall", "metacog@metacog"], check=False)
        return
    path = MUSE_SETTINGS if a.agent == "muse" else CODEX_HOOKS
    if not path.exists():
        return print("nothing to remove")
    data = json.loads(path.read_text())
    data["hooks"] = _merge_hooks(data.get("hooks"), {})
    if not data["hooks"]:
        data.pop("hooks")
    _write_json(path, data)
    print(f"removed metacog hooks from {path}")


def _muse_args(args: list[str], hooks_json: str) -> list[str]:
    # `-c` is accepted by the TUI and by subcommands; keep it after a subcommand so it applies to that run.
    if args and args[0] in ("exec", "resume", "serve"):
        return [args[0], "-c", f"hooks={hooks_json}", *args[1:]]
    return ["-c", f"hooks={hooks_json}", *args]


def cmd_wrap(a):
    agent, args = a.agent, a.args
    if args and args[0] == "--":
        args = args[1:]
    binary = shutil.which(agent)
    if not binary:
        sys.exit(f"metacog: `{agent}` not found on PATH")
    run_id = time.strftime("%Y%m%d-%H%M%S") + f"-{agent}"
    env = dict(os.environ, METACOG_RUN=run_id)
    if not a.no_egress:
        _ensure_geo()
    upstream = next((os.environ[k] for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy") if os.environ.get(k)), None)
    if upstream and not a.no_egress:
        # shortcut: the recorder connects directly, so it would break an existing proxy chain; chain to it if users need both.
        print(f"metacog: HTTPS_PROXY is already set ({upstream}); egress map disabled for this run", file=sys.stderr)
        a.no_egress = True
    if not a.no_egress:
        proxy = f"http://127.0.0.1:{egress.start(run_id, enforce=a.egress_enforce)}"
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            env[k] = proxy
        no_proxy = ",".join(filter(None, [os.environ.get("NO_PROXY") or os.environ.get("no_proxy"), "localhost,127.0.0.1,::1"]))
        env["NO_PROXY"] = env["no_proxy"] = no_proxy
    if not a.no_dashboard:
        from metacog import dashboard
        url = dashboard.start_background(a.port)
        print(f"metacog: dashboard {url}", file=sys.stderr)

    cleanup = None
    if agent == "claude":
        fd, settings = tempfile.mkstemp(prefix="metacog-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"hooks": hooks_config("claude")}, f)
        cmd, cleanup = [binary, "--settings", settings, *args], settings
    elif agent == "muse":
        cmd = [binary, *_muse_args(args, json.dumps(hooks_config("muse", headless=bool(args) and args[0] == "exec")))]
    else:
        if not (CODEX_HOOKS.exists() and "metacog hook" in CODEX_HOOKS.read_text()):
            sys.exit("metacog: Codex has no per-run hook flag; run `metacog install codex` first")
        cmd = [binary, *args]

    started = time.time()
    # Ctrl-C goes to the whole foreground process group; let the agent handle it and shut down cleanly.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        code = subprocess.Popen(cmd, env=env, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL)).wait()
    finally:
        signal.signal(signal.SIGINT, previous)
        if cleanup:
            os.unlink(cleanup)
    _print_run_summary(started, run_id, a.no_egress)
    sys.exit(code)


def _print_run_summary(since: float, run_id: str, no_egress: bool) -> None:
    lines = []
    for p in store.sessions():
        if p.stat().st_mtime < since:
            break
        recs = [r for r in store.read(p.stem) if r.get("ts", 0) >= since]
        verdicts = [r for r in recs if r.get("event") == "Verdict"]
        guards = [r for r in recs if r.get("event") == "Guard" and r.get("decision") in ("deny", "ask")]
        if not (verdicts or guards):
            continue
        last = verdicts[-1] if verdicts else {}
        false = sum(c["verdict"] == "false" for v in verdicts for c in v.get("claims", []))
        lines.append(f"  session {p.stem[:8]}: {len(verdicts)} verified turns, {false} false claims, "
                     f"{len(guards)} guarded actions, trust {last.get('trust', '-')}")
        if any(v.get("same_family") for v in verdicts):
            lines.append("    note: the verifier is from the same model family as this agent; judges tend to favour their own family")
    if not no_egress:
        seen = egress.summary(run_id)
        for h in (h for h in seen if h.get("country")):
            lines.append(f"  {'BLOCKED' if h['blocked'] else 'watched'} {h['host']} ({h['count']}): {h['why']}")
        outside = [h for h in seen if not h["allowed"] and not h.get("country")]
        if outside:
            lines.append("  hosts outside allow list: " + ", ".join(f"{h['host']} ({h['count']})" for h in outside[:8]))
    if lines:
        print("metacog summary:\n" + "\n".join(lines), file=sys.stderr)


def cmd_dashboard(a):
    import webbrowser
    from metacog import dashboard
    if not a.no_open:
        webbrowser.open(f"http://127.0.0.1:{a.port}")
    dashboard.serve(a.port)


def cmd_sessions(a):
    if not store.sessions():
        print(f"no sessions yet in {store.SESSIONS}; start one with `metacog wrap claude` or `metacog wrap muse`")
    for p in store.sessions()[: a.n]:
        recs = store.read(p.stem)
        v = [r for r in recs if r.get("event") == "Verdict"]
        g = [r for r in recs if r.get("event") == "Guard" and r.get("decision") in ("deny", "ask")]
        first = recs[0] if recs else {}
        print(f"{p.stem}  {first.get('agent', '?'):<6} {time.strftime('%m-%d %H:%M', time.localtime(first.get('ts', 0)))}  "
              f"turns={len(v)} guarded={len(g)} trust={v[-1].get('trust') if v else '-'}  {first.get('cwd', '')}")


def cmd_show(a):
    matches = [p for p in store.sessions() if p.stem.startswith(a.session or "")]  # newest first
    if not matches:
        sys.exit("no such session")
    report = None
    for r in store.read(matches[0].stem):
        if r.get("event") == "Stop" and r.get("message"):
            report = "[report] " + "\n         ".join(r["message"].strip()[:600].splitlines())
            if not a.sent_back:
                print(report)
        elif r.get("event") == "Guard" and not a.sent_back:
            print(f"[guard:{r['decision']}] {r['rule']}: {r['reason']}" + (f"\n         {r['target']}" if r.get("target") else ""))
        elif r.get("event") == "Verdict" and (r.get("blocked") or not a.sent_back):
            if a.sent_back and report:
                print(report)
            print(f"[verdict] trust={r.get('trust')} blocked={r.get('blocked')}")
            for c in r.get("claims", []):
                if not a.sent_back or c["verdict"] != "verified":
                    print(f"   {c['verdict']:<13} {c['claim']}  — {c['explanation']}")
            for f in r.get("findings", []):
                print(f"   {f['severity']:<13} {f['detail']}")
            for w in r.get("waste", []) if not a.sent_back else []:
                print(f"   waste         {w['detail']} (~{w.get('tokens', 0):,} tokens) → {w['tip']}")


def cmd_tips(a):
    from metacog import optimize
    print(optimize.tips(store.SESSIONS, os.path.realpath(a.project), a.min_sessions))


def main(argv=None):
    p = argparse.ArgumentParser(prog="metacog", description="Catch false claims, suspicious actions and waste in coding-agent sessions.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("wrap", help="run an agent with metacog attached for this session only")
    s.add_argument("agent", choices=AGENTS)
    s.add_argument("--port", type=int, default=8977, help="dashboard port")
    s.add_argument("--no-dashboard", action="store_true", help="do not start the dashboard")
    s.add_argument("--no-egress", action="store_true", help="do not route agent traffic through the egress recorder")
    s.add_argument("--egress-enforce", action="store_true", help="also refuse hosts that are not on allow_hosts (countries are blocked either way)")
    s.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the agent")
    s.set_defaults(fn=cmd_wrap)

    s = sub.add_parser("install", help="attach metacog to every session of an agent")
    s.add_argument("agent", choices=AGENTS)
    s.add_argument("--source", help="Claude plugin marketplace source (default: song2h/metacog)")
    s.set_defaults(fn=cmd_install)
    s = sub.add_parser("uninstall", help="detach metacog from an agent")
    s.add_argument("agent", choices=AGENTS)
    s.set_defaults(fn=cmd_uninstall)

    s = sub.add_parser("hook")  # internal: called by the agent's hook runner
    s.add_argument("agent", choices=AGENTS)
    s.set_defaults(fn=cmd_hook)

    sub.add_parser("init", help="store the Meta Model API key and check setup").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="check setup").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("dashboard", help="open the local dashboard (sessions, verdicts, guard decisions, egress)")
    s.add_argument("--port", type=int, default=8977, help="port on 127.0.0.1 (default 8977)")
    s.add_argument("--no-open", action="store_true", help="do not open a browser")
    s.set_defaults(fn=cmd_dashboard)
    s = sub.add_parser("sessions", help="list recent sessions")
    s.add_argument("-n", type=int, default=20, help="how many (default 20)")
    s.set_defaults(fn=cmd_sessions)
    s = sub.add_parser("show", help="print verdicts and guard decisions for a session")
    s.add_argument("session", nargs="?", help="session id or prefix (default: the latest session)")
    s.add_argument("--sent-back", action="store_true", help="only the reports metacog sent back, with their unverified claims")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("tips", help="draft CLAUDE.md / AGENTS.md lines from waste that repeats across a project's sessions")
    s.add_argument("--project", default=".", help="project directory (default: current)")
    s.add_argument("--min-sessions", type=int, default=2, help="report waste seen in at least this many sessions (default 2)")
    s.set_defaults(fn=cmd_tips)

    a = p.parse_args(argv)
    a.fn(a)
