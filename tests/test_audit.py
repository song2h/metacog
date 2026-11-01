"""Regression tests for defects found in the 2026-11-01 audit."""

import pytest

from metacog import guard, hook, store

CFG = store.DEFAULT_CONFIG


def pre(cmd=None, tool="Bash", **inp):
    if cmd is not None:
        inp["command"] = cmd
    return hook.normalize("claude", {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": tool, "tool_input": inp})


def decision(rec, history=(), cfg=CFG):
    d = guard.check(rec, list(history), cfg)
    return d and (d["decision"], d["rule"])


@pytest.mark.parametrize("cmd", ["bash -c 'rm -rf ~'", 'sh -c "rm -rf /"', "/usr/bin/rm -rf ~", "env rm -rf ~", "time rm -rf ~",
                                 "sudo -u root rm -rf /", "rm -rf /Users/alice", "rm -rf /home/bob/", ["bash", "-lc", "rm -rf ~"]])
def test_wrapped_rm_of_root_or_home_is_denied(cmd):
    assert decision(pre(cmd)) == ("deny", "rm-root")


@pytest.mark.parametrize("cmd", ["rm -rf ./build", "rm -rf ~/project/node_modules", "rm -rf /tmp/x", "bash -c 'rm -rf dist'"])
def test_ordinary_rm_is_not_rm_root(cmd):
    assert decision(pre(cmd)) is None
