"""Tests for the secrets loader."""

from __future__ import annotations

import pytest

from tts_overlay import secrets


@pytest.fixture(autouse=True)
def clear_secrets_cache(monkeypatch):
    monkeypatch.setattr(secrets, "_cache", None)
    yield
    monkeypatch.setattr(secrets, "_cache", None)


def test_env_var_takes_priority(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "secrets.env")
    monkeypatch.setenv("MY_KEY", "from_env")
    assert secrets.get("MY_KEY") == "from_env"


def test_reads_from_file(monkeypatch, tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("MY_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setattr(secrets, "SECRETS_PATH", path)
    monkeypatch.delenv("MY_KEY", raising=False)
    assert secrets.get("MY_KEY") == "from_file"


def test_set_writes_owner_only_file(monkeypatch, tmp_path):
    path = tmp_path / "secrets.env"
    monkeypatch.setattr(secrets, "SECRETS_PATH", path)
    secrets.set("MY_KEY", "from_set")
    assert secrets.get("MY_KEY") == "from_set"
    assert path.stat().st_mode & 0o777 == 0o600


def test_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "nope.env")
    monkeypatch.delenv("MISSING", raising=False)
    assert secrets.get("MISSING", "fallback") == "fallback"


def test_strips_quotes(monkeypatch, tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text('KEY1="double"\nKEY2=\'single\'\n', encoding="utf-8")
    monkeypatch.setattr(secrets, "SECRETS_PATH", path)
    monkeypatch.delenv("KEY1", raising=False)
    monkeypatch.delenv("KEY2", raising=False)
    assert secrets.get("KEY1") == "double"
    assert secrets.get("KEY2") == "single"


def test_ignores_comments_and_blanks(monkeypatch, tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text(
        "# a comment\n\n  \nREAL=value\n# another\n", encoding="utf-8"
    )
    monkeypatch.setattr(secrets, "SECRETS_PATH", path)
    monkeypatch.delenv("REAL", raising=False)
    assert secrets.get("REAL") == "value"


def test_missing_file_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(secrets, "SECRETS_PATH", tmp_path / "absent.env")
    monkeypatch.delenv("ANYTHING", raising=False)
    assert secrets.get("ANYTHING") is None


def test_set_updates_cache_and_writes_file(monkeypatch, tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("# comment\nEXISTING=123\n", encoding="utf-8")
    monkeypatch.setattr(secrets, "SECRETS_PATH", path)
    monkeypatch.delenv("EXISTING", raising=False)
    monkeypatch.delenv("NEW_KEY", raising=False)

    # Update existing key
    secrets.set("EXISTING", "456")
    assert secrets.get("EXISTING") == "456"

    # Add new key
    secrets.set("NEW_KEY", "abc")
    assert secrets.get("NEW_KEY") == "abc"

    # Verify file content
    content = path.read_text(encoding="utf-8")
    assert "# comment" in content
    assert "EXISTING=456" in content
    assert "NEW_KEY=abc" in content
