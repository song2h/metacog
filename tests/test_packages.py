"""Hallucinated-dependency check before installs."""

import time

from metacog import hook, packages, store

CFG = store.DEFAULT_CONFIG


import pytest


@pytest.fixture(autouse=True)
def public_index(monkeypatch):
    monkeypatch.setattr(packages, "private_index", lambda eco, cwd="", name="": False)


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


REAL = {("pypi", n) for n in ("requests", "torch", "yarn-api-client", "fastapi", "uvicorn", "sqlalchemy", "pydantic", "alembic")}


def fake_registry(eco, name, timeout=3):
    return (True, 1.3e9) if (eco, name) in REAL else (False, None)


@pytest.mark.parametrize("cmd", ["pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org requests",
                                 "pip install --default-timeout 100 torch", "pip install yarn-api-client",
                                 "npm install @acme/shared -w apps/web"])
def test_normal_installs_are_not_denied(monkeypatch, cmd):
    monkeypatch.setattr(packages, "lookup", fake_registry)
    assert packages.check(cmd, CFG) is None


def test_every_name_is_checked_and_a_missing_one_beats_a_new_one(monkeypatch):
    monkeypatch.setattr(packages, "lookup", fake_registry)
    d = packages.check("pip install fastapi uvicorn sqlalchemy pydantic alembic fastapi-jwt-auth-pro-helpers", CFG)
    assert (d["decision"], d["package"]) == ("deny", "fastapi-jwt-auth-pro-helpers")
    monkeypatch.setattr(packages, "lookup", lambda eco, name, timeout=3: (True, time.time()) if name == "new-lib" else fake_registry(eco, name))
    assert packages.check("pip install new-lib fastapi-jwt-auth-pro-helpers", CFG)["decision"] == "deny"


def test_slow_registry_lookups_stay_within_the_budget(monkeypatch):
    monkeypatch.setattr(packages, "BUDGET", 0.2)
    monkeypatch.setattr(packages, "lookup", lambda eco, name, timeout=3: time.sleep(3) or (False, None))
    t0 = time.time()
    assert packages.check("pip install a1 a2 a3 a4 a5 a6 a7 a8", CFG) is None  # unanswered in time: never block
    assert time.time() - t0 < 1.5


def test_scope_with_its_own_registry_is_private(monkeypatch, tmp_path):
    monkeypatch.undo()  # use the real private_index
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in packages.PRIVATE_ENV:
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".npmrc").write_text("@acme:registry=https://npm.acme.internal/\n")
    monkeypatch.setattr(packages, "lookup", lambda eco, name, timeout=3: (False, None))
    assert packages.check("npm install @acme/ui", CFG, str(tmp_path)) is None
    assert packages.check("npm install @acme/ui left-padd", CFG, str(tmp_path))["package"] == "left-padd"
