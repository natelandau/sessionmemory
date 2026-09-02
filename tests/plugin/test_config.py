"""Verify the flat SessionMemoryConfig cascade and the headless guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sessionhooks.config import SessionMemoryConfig  # ty: ignore[unresolved-import]
from sessionhooks.headless import HEADLESS_ENV, is_headless  # ty: ignore[unresolved-import]

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_load_defaults_when_no_files(tmp_path: Path) -> None:
    """Verify an absent config yields the documented defaults."""
    # Given no config files anywhere
    # When config loads with isolated home/project
    cfg = SessionMemoryConfig.load(home=tmp_path / "home", project_dir=str(tmp_path / "proj"))

    # Then every field falls back to its default
    assert cfg.inject_enabled is True
    assert cfg.sweep_enabled is True
    assert cfg.sweep_model == "claude-sonnet-4-6"
    assert cfg.min_exchanges == 10
    assert cfg.sweep_save_transcript is True


def test_save_transcript_can_be_disabled(tmp_path: Path) -> None:
    """Verify save_transcript = false is read from the [sweep] table."""
    # Given a project config that opts out of saving the sweep transcript
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        "[sweep]\nsave_transcript = false\n", encoding="utf-8"
    )

    # When config loads
    cfg = SessionMemoryConfig.load(home=tmp_path / "home", project_dir=str(proj))

    # Then the flag reflects the opt-out
    assert cfg.sweep_save_transcript is False


def test_project_overrides_win(tmp_path: Path) -> None:
    """Verify project config overrides global per key while keeping unset defaults."""
    # Given a global config and a project config that overrides a subset of keys
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "sessionmemory.toml").write_text(
        '[sweep]\nmodel = "claude-global"\nmin_exchanges = 9\n', encoding="utf-8"
    )
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        "[inject]\nenabled = false\n[sweep]\nmin_exchanges = 3\n", encoding="utf-8"
    )

    # When config loads with both layers present
    cfg = SessionMemoryConfig.load(home=home, project_dir=str(proj))

    # Then project keys win, global-only keys persist, and untouched keys default
    assert cfg.inject_enabled is False  # from project
    assert cfg.min_exchanges == 3  # project overrides global
    assert cfg.sweep_model == "claude-global"  # global, not overridden by project
    assert cfg.sweep_enabled is True  # default, set nowhere


def test_malformed_toml_fails_open_to_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify a malformed config file is ignored (warned) and defaults apply."""
    # Given a project config that is not valid TOML
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text("this is = = not toml", encoding="utf-8")

    # When config loads
    cfg = SessionMemoryConfig.load(home=tmp_path / "home", project_dir=str(proj))

    # Then the broken file is ignored and defaults stand, with a stderr warning
    assert cfg.min_exchanges == 10
    assert cfg.inject_enabled is True
    assert "sessionmemory" in capsys.readouterr().err


def test_bad_value_types_fall_back(tmp_path: Path) -> None:
    """Verify a non-int min_exchanges falls back to the default rather than raising."""
    # Given a project config with a wrongly-typed value
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        '[sweep]\nmin_exchanges = "not-an-int"\n', encoding="utf-8"
    )

    # When config loads
    cfg = SessionMemoryConfig.load(home=tmp_path / "home", project_dir=str(proj))

    # Then the bad value is dropped for the default
    assert cfg.min_exchanges == 10


def test_is_headless_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify is_headless reflects the SESSIONMEMORY_HEADLESS env var."""
    # Given the env unset
    monkeypatch.delenv(HEADLESS_ENV, raising=False)
    assert is_headless() is False

    # When the guard env is set to 1
    monkeypatch.setenv(HEADLESS_ENV, "1")

    # Then the guard reports headless
    assert is_headless() is True


def test_vault_root_is_read_from_the_config(tmp_path):
    """Verify vault_root reads the [vault] root key from the global config."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "sessionmemory.toml").write_text(
        '[vault]\nroot = "~/repos/project-memory"\n', encoding="utf-8"
    )
    assert SessionMemoryConfig.load(home=home).vault_root == "~/repos/project-memory"


def test_vault_root_defaults_to_empty_when_unset(tmp_path):
    """Verify vault_root is empty when no config sets it."""
    assert SessionMemoryConfig.load(home=tmp_path).vault_root == ""


def test_a_project_config_overrides_the_global_vault_root(tmp_path):
    """Verify a project config's [vault] root wins over the global one."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "sessionmemory.toml").write_text(
        '[vault]\nroot = "/global"\n', encoding="utf-8"
    )
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "sessionmemory.toml").write_text(
        '[vault]\nroot = "/project"\n', encoding="utf-8"
    )
    assert SessionMemoryConfig.load(home=home, project_dir=str(proj)).vault_root == "/project"


def test_substance_floors_have_defaults(tmp_path):
    """Verify the human-substance floors carry defaults when no config file exists."""
    cfg = SessionMemoryConfig.load(home=tmp_path)
    assert cfg.min_user_messages == 3
    assert cfg.min_user_chars == 400


def test_substance_floors_are_read_from_the_config(tmp_path):
    """Verify both substance floors are configurable under [sweep]."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "sessionmemory.toml").write_text(
        "[sweep]\nmin_user_messages = 5\nmin_user_chars = 1200\n", encoding="utf-8"
    )
    cfg = SessionMemoryConfig.load(home=home)
    assert cfg.min_user_messages == 5
    assert cfg.min_user_chars == 1200


def test_substance_floors_fall_back_on_a_bad_value(tmp_path):
    """Verify a non-integer floor uses the default rather than wedging the sweep."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "sessionmemory.toml").write_text(
        '[sweep]\nmin_user_messages = true\nmin_user_chars = "lots"\n', encoding="utf-8"
    )
    cfg = SessionMemoryConfig.load(home=home)
    assert cfg.min_user_messages == 3
    assert cfg.min_user_chars == 400
