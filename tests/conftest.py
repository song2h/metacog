import pytest

from metacog import geo, store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own ~/.metacog and no real API key."""
    monkeypatch.setattr(store, "HOME", tmp_path / "home")
    monkeypatch.setattr(store, "SESSIONS", tmp_path / "home" / "sessions")
    monkeypatch.setattr(store, "CONFIG_FILE", tmp_path / "home" / "config.json")
    monkeypatch.setattr(store, "ENV_FILE", tmp_path / "no.env")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("METACOG_BLOCK_COUNTRIES", raising=False)
    monkeypatch.setattr(geo, "_ranges", None)
    return tmp_path
