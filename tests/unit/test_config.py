"""Tests for vault root discovery."""

from __future__ import annotations

import pytest

from sessionmemory.lib import config
from sessionmemory.lib.bootstrap import initialize
from sessionmemory.lib.config import (
    VaultNotConfiguredError,
    is_initialized,
    vault_root,
)


def test_vault_root_reads_environment(tmp_path, monkeypatch):
    """Return the directory named by SESSIONMEMORY_VAULT."""
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path))
    assert vault_root() == tmp_path


def test_vault_root_expands_user(tmp_path, monkeypatch):
    """Expand a leading tilde so `~/vault` works in a shell profile."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SESSIONMEMORY_VAULT", "~/somevault")
    (tmp_path / "somevault").mkdir()
    assert vault_root() == tmp_path / "somevault"


def test_vault_root_unset_raises(monkeypatch):
    """Fail loudly when the variable is unset rather than guessing a location."""
    monkeypatch.delenv("SESSIONMEMORY_VAULT", raising=False)
    with pytest.raises(VaultNotConfiguredError, match="SESSIONMEMORY_VAULT"):
        vault_root()


def test_vault_root_missing_directory_raises(tmp_path, monkeypatch):
    """Fail when the variable points at something that does not exist."""
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(tmp_path / "nope"))
    with pytest.raises(VaultNotConfiguredError, match="does not exist"):
        vault_root()


def test_now_is_second_precision_utc_with_z_suffix():
    """Verify now() renders as YYYY-MM-DDTHH:MM:SSZ."""
    import re

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", config.now())


def test_is_initialized_false_without_a_marker(tmp_path):
    """Refuse to treat a directory that merely exists as a vault."""
    assert is_initialized(tmp_path) is False


def test_is_initialized_true_after_initialize(tmp_path):
    """Recognize a vault once `initialize` has written its marker."""
    initialize(tmp_path)
    assert is_initialized(tmp_path) is True


def test_is_initialized_false_for_a_corrupt_marker(tmp_path):
    """Treat an unparsable marker as absent rather than as proof of a vault."""
    system_dir = tmp_path / "_system"
    system_dir.mkdir()
    (system_dir / "vault.toml").write_text("this is not [ valid toml", encoding="utf-8")

    assert is_initialized(tmp_path) is False
