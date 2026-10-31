"""Hook entry point shared by Claude Code, Muse Code and Codex.

All three agents send the same hook shape (verified for Claude Code 2.1 and Muse Code 1.4;
Codex per its docs): JSON on stdin with session_id, hook_event_name, tool_name, tool_input,
tool_response, last_assistant_message. Decisions go back as JSON on stdout.
"""

import json
import re
import sys
import time
import traceback

from metacog import guard, optimize, store, verify

SHELL_TOOLS = {"bash", "shell", "exec_command", "local_shell", "run_shell_command", "terminal"}
# Muse Code runs internal bookkeeping tools through the same hooks; they are not agent actions.
INTERNAL_TOOLS = {"submit_reminder_decision"}
OUTPUT_LIMIT = 8000


def clip(s: str, n: int = OUTPUT_LIMIT) -> str:
    """Keep head and tail: test summaries and errors live at the end of long outputs."""
    return s if len(s) <= n else s[: n // 4] + f"\n...[{len(s) - n} chars clipped]...\n" + s[-(n * 3 // 4):]


def _text(v) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _written(inp: dict) -> str:
    parts = [inp.get(k) for k in ("content", "new_string", "patch", "input", "new_source")]
    parts += [e.get("new_string") for e in inp.get("edits") or [] if isinstance(e, dict)]
    return "\n".join(p for p in parts if isinstance(p, str))


def _path(inp: dict) -> str:
    p = inp.get("file_path") or inp.get("path") or inp.get("filename") or inp.get("notebook_path")
    if not p:
        m = re.search(r"\*\*\* (?:Add|Update|Delete) File: (\S+)", _written(inp))
        p = m.group(1) if m else ""
    return p if isinstance(p, str) else ""


def _kind(tool: str, inp: dict) -> str:
    t = tool.lower()
    if t in SHELL_TOOLS or ("command" in inp and not any(w in t for w in ("edit", "write"))):
        return "shell"
    if re.search(r"write|edit|patch|create|notebook", t) or _written(inp):
        return "write"
    if re.search(r"read|view|cat|open", t):
        return "read"
    return "other"


def _result(event: str, ev: dict) -> tuple[bool, int | None, str]:
    """(ok, exit_code, output) from PostToolUse / PostToolUseFailure."""
    if event == "PostToolUseFailure":
        err = _text(ev.get("error") or ev.get("tool_response"))
        m = re.match(r"Exit code (\d+)", err)
        return False, int(m.group(1)) if m else None, err
    resp = ev.get("tool_response")
    if isinstance(resp, str):  # Muse Code sends the shell result as a JSON string
        try:
            resp = json.loads(resp)
        except json.JSONDecodeError:
            return True, None, resp
    if isinstance(resp, dict):
        code = resp.get("exit_code", resp.get("exitCode"))
        out = resp.get("output")
        if out is None:
            out = "\n".join(_text(resp.get(k)) for k in ("stdout", "stderr") if resp.get(k))
        if out == "" and "stdout" not in resp:
            out = _text(resp)
        ok = (code in (None, 0)) and not resp.get("interrupted") and not resp.get("is_error")
        return ok, code, _text(out)
    return True, None, _text(resp)


def normalize(agent: str, ev: dict) -> dict | None:
    event = ev.get("hook_event_name", "")
    rec = {"agent": agent, "event": event, "session": ev.get("session_id", "unknown"), "cwd": ev.get("cwd", "")}
    if ev.get("model"):
        rec["model"] = ev["model"]
    if event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        tool = ev.get("tool_name") or ""
        if tool in INTERNAL_TOOLS:
            return None
        inp = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {"input": _text(ev.get("tool_input"))}
        cmd = inp.get("command", "")
        rec.update(tool=tool, kind=_kind(tool, inp), tool_use_id=ev.get("tool_use_id"),
                   command=" ".join(cmd) if isinstance(cmd, list) else _text(cmd),
                   path=_path(inp), content=clip(_written(inp)))
        if event != "PreToolUse":
            ok, code, out = _result(event, ev)
            rec.update(ok=ok, exit_code=code, output=clip(out), output_len=len(out))
    elif event == "UserPromptSubmit":
        rec["prompt"] = clip(_text(ev.get("prompt")))
    elif event == "Stop":
        rec.update(message=_text(ev.get("last_assistant_message")), stop_hook_active=bool(ev.get("stop_hook_active")))
    return rec


def handle(agent: str, ev: dict) -> dict | None:
    """Record the event and return the JSON decision to print, if any."""
    rec = normalize(agent, ev)
    if rec is None:
        return None
    sid = rec["session"]
    history = store.read(sid)
    store.append(sid, rec)

    if rec["event"] == "PreToolUse":
        decision = guard.check(rec, history, store.config())
        if decision:
            store.append(sid, {"event": "Guard", "agent": agent, "session": sid, "tool_use_id": rec.get("tool_use_id"), **decision})
            if decision["decision"] in ("deny", "ask"):
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision["decision"],
                                               "permissionDecisionReason": f"metacog: {decision['reason']}"}}
        return None

    if rec["event"] == "Stop":
        turn = verify.current_turn(history)
        t0 = time.time()
        result = verify.run(rec.get("message", ""), turn, rec.get("cwd", ""), store.config())
        result["waste"] = optimize.analyze(turn)
        blocks_so_far = sum(1 for r in turn if r.get("event") == "Verdict" and r.get("blocked"))
        block = result["problems"] and blocks_so_far < store.config()["max_blocks_per_turn"]
        store.append(sid, {"event": "Verdict", "agent": agent, "session": sid, "blocked": bool(block),
                           "latency_ms": int((time.time() - t0) * 1000), **result})
        if block:
            return {"decision": "block", "reason": verify.feedback(result)}
    return None


def main(agent: str) -> None:
    try:
        ev = json.load(sys.stdin)
        out = handle(agent, ev)
        if out:
            print(json.dumps(out, ensure_ascii=False))
    except Exception:
        # Never break the agent because the supervisor failed; leave a trace instead.
        store.HOME.mkdir(parents=True, exist_ok=True)
        with (store.HOME / "errors.log").open("a") as f:
            f.write(f"{time.ctime()} {agent}\n{traceback.format_exc()}\n")
