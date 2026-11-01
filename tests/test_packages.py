"""Hallucinated-dependency check before installs."""

import time

from metacog import hook, packages, store

CFG = store.DEFAULT_CONFIG


import pytest


@pytest.fixture(autouse=True)
def public_index(monkeypatch):
    monkeypatch.setattr(packages, "private_index", lambda eco, cwd="": False)


def test_option_values_and_files_are_not_package_names():
    assert packages.requested("poetry add --group dev pytest") == [("pypi", "pytest")]
    assert packages.requested("uv add --optional cli typer") == [("pypi", "typer")]
    assert packages.requested("uv pip install --python 3.12 rich") == [("pypi", "rich")]
    assert packages.requested("pip install --log install.log rich foo-1.0-py3-none-any.whl") == [("pypi", "rich")]
    assert packages.requested("npm install local-pkg.tgz && npm i -D typescript") == [("npm", "typescript")]
    assert packages.requested("python3.12 -m pip install httpx") == [("pypi", "httpx")]


def test_private_index_from_environment(monkeypatch):
    monkeypatch.undo()  # use the real private_index
    monkeypatch.setenv("PIP_INDEX_URL", "https://pypi.internal/simple")
    assert packages.private_index("pypi")


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


def test_run_a_package_commands_name_only_the_package(tmp_path):
    assert packages.requested("uvx ruff check .") == [("pypi", "ruff")]
    assert packages.requested("uvx -p 3.12 --from 'httpie==3.2' http GET x") == [("pypi", "httpie")]
    assert packages.requested("uv tool install black@24.1 && pipx run cowsay hi") == [("pypi", "black"), ("pypi", "cowsay")]
    assert packages.requested("npx -y create-vite@5 app --template react") == [("npm", "create-vite")]
    assert packages.requested("npx --package=@scope/tool@1 -- tool --x") == [("npm", "@scope/tool")]
    assert packages.requested("pnpm dlx degit user/repo") == [("npm", "degit")]
    (tmp_path / "node_modules/.bin").mkdir(parents=True)
    (tmp_path / "node_modules/.bin/tsc").touch()
    assert packages.requested("npx tsc --noEmit", str(tmp_path)) == []  # the project's own binary
    assert packages.requested("npx ./scripts/x.js") == []


def test_runner_of_a_missing_package_is_denied(monkeypatch):
    monkeypatch.setattr(packages, "lookup", lambda eco, name, timeout=3: (False, None))
    assert packages.check("uvx fastapi-jwt-auth-pro-helpers", CFG)["rule"] == "package-missing"
