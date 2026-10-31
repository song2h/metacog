"""Hallucinated-dependency check before installs."""

import time

from metacog import hook, packages, store

CFG = store.DEFAULT_CONFIG


def test_requested_names():
    assert packages.requested("pip install requests==2.31 'fastapi[all]>=0.1' -r req.txt ./local git+https://x/y.git") == \
        [("pypi", "requests"), ("pypi", "fastapi")]
    assert packages.requested("npm i @scope/pkg@1.2 left-pad@latest --save-dev && npm test") == [("npm", "@scope/pkg"), ("npm", "left-pad")]
    assert packages.requested("pip install --index-url https://pypi.internal/simple corp-lib") == []
    assert packages.requested("pip download requests") == []


def test_missing_denied_new_asked_old_allowed(monkeypatch):
    registry = {"fastapi-auth-helpers": (False, None), "brand-new-lib": (True, time.time() - 3 * 86400), "requests": (True, 1.3e9)}
    monkeypatch.setattr(packages, "lookup", lambda eco, name, timeout=3: registry.get(name))
    assert packages.check("pip install fastapi-auth-helpers", CFG)["decision"] == "deny"
    assert packages.check("uv add brand-new-lib", CFG)["decision"] == "ask"
    assert packages.check("pip install requests", CFG) is None
    assert packages.check("pip install unknown-when-offline", CFG) is None  # registry unreachable: never block
    assert packages.check("pip install fastapi-auth-helpers", {**CFG, "check_packages": False}) is None


def test_hook_returns_the_package_decision(monkeypatch):
    monkeypatch.setattr(packages, "lookup", lambda eco, name, timeout=3: (False, None))
    out = hook.handle("claude", {"hook_event_name": "PreToolUse", "session_id": "pk", "tool_name": "Bash",
                                 "tool_input": {"command": "pip install fastapi-auth-helpers"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "hallucinated" in out["hookSpecificOutput"]["permissionDecisionReason"]
