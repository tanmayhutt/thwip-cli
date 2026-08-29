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
