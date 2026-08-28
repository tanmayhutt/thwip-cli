"""
Unit tests for thwip session persistence and cross-agent context portability.
"""

from __future__ import annotations

import pytest

from thwip.config import ThwipConfig, get_config_path
from thwip.session import Session


def test_session_message_flow():
    session = Session(name="test-session")
    session.add_user_message("Hello from user")
    session.add_assistant_message("Hello back", agent_name="claude", model="claude-sonnet-4")

    assert len(session.messages) == 2
    assert session.messages[0].role == "user"
    assert session.messages[0].content == "Hello from user"
    assert session.messages[1].role == "assistant"
    assert session.messages[1].agent_name == "claude"

    # Verify portable representation
    portable = session.to_portable_messages()
    assert len(portable) == 2
    assert portable[0] == {"role": "user", "content": "Hello from user"}
    assert portable[1] == {"role": "assistant", "content": "Hello back"}


def test_session_agent_switching():
    session = Session(current_agent="claude", current_model="claude-sonnet-4")
    session.add_user_message("Write a python function")
    session.add_assistant_message("def hello(): pass", agent_name="claude", model="claude-sonnet-4")

    # Switch agent to google/gemini
    session.switch_agent("google", "gemini-2.5-pro")

    assert session.current_agent == "google"
    assert session.current_model == "gemini-2.5-pro"
    # Context must be fully preserved
    assert len(session.messages) == 2


def test_session_serialization(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path / "config"))
    session = Session(name="serialize-test", project_path="/tmp/test")
    session.add_user_message("How are you?")
    session.add_assistant_message("Doing great!", agent_name="openai", model="gpt-4.1")

    saved_path = session.save("serialize-test")
    assert saved_path.is_file()

    loaded = Session.load("serialize-test")
    assert loaded is not None
    assert loaded.name == "serialize-test"
    assert loaded.project_path == "/tmp/test"
    assert len(loaded.messages) == 2
    assert loaded.messages[1].agent_name == "openai"
    assert saved_path.stat().st_mode & 0o777 == 0o600


def test_session_name_cannot_escape_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path / "config"))
    session = Session(name="safe")

    saved_path = session.save("../../outside")

    assert saved_path.parent == tmp_path / "config" / "sessions"
    assert saved_path.name == "outside.json"
    assert not (tmp_path / "outside.json").exists()


def test_empty_session_name_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path / "config"))

    with pytest.raises(ValueError):
        Session(name="...").save()


def test_config_file_permissions_and_source_are_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path / "config"))
    config = ThwipConfig(keys={"openai": "test-key"}, key_sources={"openai": "config.toml"})
    config.save()

    loaded = ThwipConfig.load()

    assert loaded.key_sources["openai"] == "config.toml"
    assert get_config_path().stat().st_mode & 0o777 == 0o600
