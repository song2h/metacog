"""Waste detection: tool calls that burn context and time without moving the task forward.

analyze() runs at the end of each turn and puts a token estimate on every finding (about 4
characters per token, from the recorded output sizes). tips() looks across all sessions of
one project and turns the waste that keeps coming back into lines for CLAUDE.md / AGENTS.md,
so the next session avoids it instead of being told about it afterwards.
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

BIG_OUTPUT = 20000
CHARS_PER_TOKEN = 4
QUIET = [("pytest", "pytest -q (add -x to stop at the first failure)"), ("npm test", "npm test --silent"),
         ("npm install", "npm install --no-audit --no-fund --loglevel=error"), ("pip install", "pip install -q"),
         ("cargo build", "cargo build -q"), ("go test", "go test ./... 2>&1 | tail -40"), ("docker build", "docker build -q"),
         ("kubectl describe", "kubectl get -o jsonpath=... for the fields you need"), ("git log", "git log --oneline -n 20")]


def _tokens(chars: int) -> int:
    return chars // CHARS_PER_TOKEN


def _size(r: dict) -> int:
    return r.get("output_len") or len(r.get("output") or "")


def quiet_hint(cmd: str) -> str:
    for prefix, hint in QUIET:
        if prefix in cmd:
            return f"use `{hint}`"
    return "pipe through `tail -40` / `grep`, or use the tool's quiet flag"


def analyze(turn: list[dict]) -> list[dict]:
    done = [r for r in turn if r.get("event") in ("PostToolUse", "PostToolUseFailure")]
    out = []

    reads = defaultdict(list)
    for r in done:
        if r.get("kind") == "read" and r.get("path"):
            reads[r["path"]].append(r)
    for path, rs in reads.items():
        if len(rs) >= 3:
            out.append({"kind": "repeated-read", "key": path, "count": len(rs), "tokens": _tokens(sum(map(_size, rs[1:]))),
                        "detail": f"read {path} {len(rs)} times",
                        "tip": "keep the relevant part in context, or read only the lines you need"})

    fails = defaultdict(list)
    for r in done:
        if r.get("kind") == "shell" and not r.get("ok") and r.get("command"):
            fails[r["command"]].append(r)
    for cmd, rs in fails.items():
        if len(rs) >= 2:
            first_error = next((l for l in (rs[-1].get("output") or "").splitlines() if l.strip()), "")[:120]
            out.append({"kind": "retry-loop", "key": cmd, "count": len(rs), "tokens": _tokens(sum(map(_size, rs[1:]))),
                        "detail": f"`{cmd[:80]}` failed {len(rs)} times", "error": first_error,
                        "tip": "read the error and change the approach before running the same command again"})

    for r in done:
        if _size(r) > BIG_OUTPUT and r.get("kind") == "shell":
            out.append({"kind": "large-output", "key": r.get("command", ""), "count": 1, "tokens": _tokens(_size(r)),
                        "detail": f"`{r.get('command', '')[:80]}` printed {_size(r):,} chars", "tip": quiet_hint(r.get("command", ""))})

    same = Counter((r.get("tool"), r.get("command") or r.get("path")) for r in done if r.get("ok"))
    for (tool, target), n in same.items():
        if n >= 3 and tool and target and target not in reads:
            size = sum(_size(r) for r in done if (r.get("command") or r.get("path")) == target)
            out.append({"kind": "duplicate-call", "key": str(target), "count": n, "tokens": _tokens(size * (n - 1) // n),
                        "detail": f"{tool} `{str(target)[:80]}` ran {n} times with success",
                        "tip": "reuse the earlier result instead of running it again"})
    return out


def tips(sessions_dir: Path, project: str, min_sessions: int = 2) -> str:
    """Markdown advice for one project from the waste recorded in its sessions."""
    seen = defaultdict(lambda: {"sessions": set(), "tokens": 0, "count": 0, "item": None})
    sessions, verify_cost, verifications = set(), 0.0, 0
    for path in sorted(sessions_dir.glob("*.jsonl")):
        recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        if not any(r.get("cwd") == project for r in recs):
            continue
        sessions.add(path.stem)
        for v in (r for r in recs if r.get("event") == "Verdict"):
            u = v.get("usage") or {}
            if u:
                verifications += 1
                verify_cost += (u.get("prompt_tokens", 0) * 1.25 + u.get("completion_tokens", 0) * 4.25) / 1e6
            for w in v.get("waste", []):
                s = seen[(w["kind"], w.get("key", w["detail"]))]
                s["sessions"].add(path.stem)
                s["tokens"] += w.get("tokens", 0)
                s["count"] += w.get("count", 1)
                s["item"] = w
    lines = [f"# metacog tips for {project}", "", f"{len(sessions)} sessions recorded."]
    if verifications:
        lines.append(f"Verification cost so far: {verifications} checks, about ${verify_cost:.3f} (Muse Spark standard pricing).")
    recurring = sorted((s for s in seen.values() if len(s["sessions"]) >= min_sessions), key=lambda s: -s["tokens"])
    if not recurring:
        lines += ["", f"No waste pattern has repeated in {min_sessions}+ sessions yet."]
        return "\n".join(lines)
    total = sum(s["tokens"] for s in recurring)
    lines += ["", f"Recurring waste: about {total:,} tokens. Suggested lines for CLAUDE.md / AGENTS.md:", ""]
    for s in recurring[:10]:
        w, n = s["item"], len(s["sessions"])
        if w["kind"] == "large-output":
            text = f"- `{w['key'][:80]}` prints a lot of output; {w['tip']}."
        elif w["kind"] == "repeated-read":
            text = f"- `{w['key']}` gets re-read in many sessions; its key facts: <summarize them here so the file need not be re-read>."
        elif w["kind"] == "retry-loop":
            text = f"- `{w['key'][:80]}` keeps failing" + (f" with `{w['error']}`" if w.get("error") else "") + "; <document the fix here>."
        else:
            text = f"- Reuse the result of `{w['key'][:80]}` instead of running it again in the same task."
        lines.append(f"{text}  _({n} sessions, ~{s['tokens']:,} tokens)_")
    return "\n".join(lines)
