"""
Unit tests for thwip session persistence and cross-agent context portability.
"""

from __future__ import annotations

import json

import pytest

from thwip.config import ThwipConfig, get_config_path
from thwip.session import Session


def test_independent_autosaves_preserve_both_conversations(tmp_path, monkeypatch):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    first, second = Session(), Session()
    first.add_user_message("first conversation")
    second.add_user_message("second conversation")
    first_path, second_path = first.save(), second.save()
    assert first_path != second_path
    assert Session.load(first.name).messages[0].content == "first conversation"
    assert Session.load(second.name).messages[0].content == "second conversation"
    assert first.save() == first_path


@pytest.mark.parametrize("field,value", [
    ("created_at", "bad"), ("updated_at", float("nan")),
    ("updated_at", float("inf")), ("updated_at", True),
    ("messages", {}), ("messages", ""),
])
def test_corrupt_session_metadata_is_rejected(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    path = Session(name="damaged").save()
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    assert Session.load("damaged") is None


@pytest.mark.parametrize("field,value", [("timestamp", -1), ("timestamp", float("nan")),
                                         ("agent_name", []), ("company", 1), ("model", None)])
def test_corrupt_message_metadata_is_rejected(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    session = Session(name="damaged")
    session.add_user_message("text")
    path = session.save()
    data = json.loads(path.read_text())
    data["messages"][0][field] = value
    path.write_text(json.dumps(data))
    assert Session.load("damaged") is None


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


def test_native_sessions_are_tracked_validated_and_persisted(tmp_path, monkeypatch):
    from thwip.session import Session

    monkeypatch.setenv("THWIP_CONFIG_DIR", str(tmp_path))
    session = Session(project_path=str(tmp_path), current_agent="openai", current_model="m")
    assert session.native_session("openai") is None
    session.add_user_message("q")
    session.add_assistant_message("a", agent_name="openai", model="m")
    session.set_native_session("openai", "thread-1", "m")
    assert session.native_session("openai") == {"id": "thread-1", "synced": 2, "model": "m"}
    session.native_sessions["claude"] = {"id": "x", "synced": 99}
    assert session.native_session("claude") is None, "a record that claims more messages than exist is ignored"
    path = session.save("native-track")
    loaded = Session.load("native-track")
    assert loaded.native_session("openai") == {"id": "thread-1", "synced": 2, "model": "m"}
    loaded.clear_context()
    assert loaded.native_sessions == {}
    path.write_text(path.read_text().replace('"synced": 2', '"synced": "2"'))
    assert Session.load("native-track") is None, "malformed native session records reject the file"
