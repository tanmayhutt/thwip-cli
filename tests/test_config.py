"""Regression tests for secure configuration persistence."""

from __future__ import annotations

import tomllib

from thwip.config import ThwipConfig


def test_save_does_not_persist_environment_or_discovered_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    config = ThwipConfig(
        keys={"openai": "from-env", "anthropic": "explicit"},
        key_sources={"openai": "env:OPENAI_API_KEY", "anthropic": "config.toml"},
    )

    config.save()

    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["keys"] == {"anthropic": "explicit"}
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600


def test_memory_and_endpoint_sections_round_trip(tmp_path, monkeypatch):
    """The section whitelist once dropped [memory] and [endpoints], so onboarding repeated and overrides were ignored."""
    import tomllib

    from thwip import endpoints
    from thwip.config import ThwipConfig

    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    config = ThwipConfig()
    config._apply_toml(tomllib.loads(
        '[memory]\nvault = "/vault"\nonboarded = true\ncards_dir = "Projects/thwip"\nscan = ["/a", "/b"]\n'
        '[endpoints]\nopenai = "https://gateway.example/v1"\nbad = 5\n'))
    assert config.memory.vault == "/vault" and config.memory.onboarded and config.memory.cards_dir == "Projects/thwip"
    assert config.memory.scan == ["/a", "/b"] and config.endpoints == {"openai": "https://gateway.example/v1"}
    assert endpoints.base_url("openai") == "https://gateway.example/v1"
    endpoints.configure({})
    config.save()
    reloaded = ThwipConfig.load()
    assert reloaded.memory.vault == "/vault" and reloaded.memory.onboarded and reloaded.memory.scan == ["/a", "/b"]
    assert reloaded.endpoints == {"openai": "https://gateway.example/v1"}
    endpoints.configure({})
