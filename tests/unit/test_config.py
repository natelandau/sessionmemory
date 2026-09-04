"""Tests for vault root discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sessionmemory.lib import config
from sessionmemory.lib.bootstrap import initialize
from sessionmemory.lib.config import (
    VaultNotConfiguredError,
    is_initialized,
    vault_root,
)

if TYPE_CHECKING:
    from pathlib import Path


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


def _write_config(monkeypatch, tmp_path, text: str) -> Path:
    """Write `text` as the config file and point `vault_root` at it."""
    path = tmp_path / "sessionmemory.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    return path


def test_vault_root_falls_back_to_the_config_file(tmp_path, monkeypatch):
    """Read `[vault] root` from the config file when the variable is unset."""
    monkeypatch.delenv("SESSIONMEMORY_VAULT", raising=False)
    root = tmp_path / "configured"
    root.mkdir()
    _write_config(monkeypatch, tmp_path, f'[vault]\nroot = "{root}"\n')

    assert vault_root() == root


def test_environment_wins_over_the_config_file(tmp_path, monkeypatch):
    """Prefer the variable when both name a vault, as the hooks do."""
    from_env = tmp_path / "from-env"
    from_env.mkdir()
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(from_env))
    _write_config(monkeypatch, tmp_path, f'[vault]\nroot = "{tmp_path / "from-config"}"\n')

    assert vault_root() == from_env


def test_config_file_root_expands_user(tmp_path, monkeypatch):
    """Expand a leading tilde in the config file, since that is how the example writes it."""
    monkeypatch.delenv("SESSIONMEMORY_VAULT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "somevault").mkdir()
    _write_config(monkeypatch, tmp_path, '[vault]\nroot = "~/somevault"\n')

    assert vault_root() == tmp_path / "somevault"


def test_config_file_root_missing_directory_raises(tmp_path, monkeypatch):
    """Name the config file when its root points at nothing, so the fix is findable."""
    monkeypatch.delenv("SESSIONMEMORY_VAULT", raising=False)
    path = _write_config(monkeypatch, tmp_path, f'[vault]\nroot = "{tmp_path / "nope"}"\n')

    with pytest.raises(VaultNotConfiguredError, match=rf"{path.name}.*does not exist"):
        vault_root()


def test_config_file_without_a_root_raises_as_unset(tmp_path, monkeypatch):
    """Treat a config file that names no root the same as no config file."""
    monkeypatch.delenv("SESSIONMEMORY_VAULT", raising=False)
    _write_config(monkeypatch, tmp_path, "[sweep]\nenabled = false\n")

    with pytest.raises(VaultNotConfiguredError, match="SESSIONMEMORY_VAULT"):
        vault_root()


def test_unparsable_config_file_raises_naming_the_file(tmp_path, monkeypatch):
    """Report a broken config file rather than silently running with no vault."""
    monkeypatch.delenv("SESSIONMEMORY_VAULT", raising=False)
    path = _write_config(monkeypatch, tmp_path, "[vault\nroot = oops")

    with pytest.raises(VaultNotConfiguredError, match=path.name):
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


def test_today_is_the_local_date(monkeypatch):
    """Verify today() follows the process's time zone rather than UTC."""
    import datetime
    import time
    import zoneinfo

    dates = {}
    # UTC-12 and UTC+14 are 26 hours apart, so they never share a calendar date.
    for zone in ("Etc/GMT+12", "Pacific/Kiritimati"):
        monkeypatch.setenv("TZ", zone)
        time.tzset()
        dates[zone] = config.today()
        assert dates[zone] == datetime.datetime.now(tz=zoneinfo.ZoneInfo(zone)).date().isoformat()
    monkeypatch.delenv("TZ")
    time.tzset()

    assert dates["Etc/GMT+12"] != dates["Pacific/Kiritimati"]
