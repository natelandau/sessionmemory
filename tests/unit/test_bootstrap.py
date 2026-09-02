"""Tests for vault initialization.

`initialize` creates the files a vault needs before anything can be written into it.
The tests here focus on the two properties that matter most: it never destroys a
hand-edited file, and its refusal to touch a non-empty non-vault directory is the whole
reason `require_vault` can trust `is_initialized` later.
"""

from __future__ import annotations

import pytest

from sessionmemory.lib.bootstrap import NotAVaultError, initialize
from sessionmemory.lib.embed import MODEL_CODE


def test_initialize_creates_all_three_files_in_an_empty_directory(tmp_path):
    """Verify a fresh directory gets the marker, gitignore, and README."""
    result = initialize(tmp_path)

    assert set(result.created) == {
        "_system/vault.toml",
        ".gitignore",
        "README.md",
    }
    assert result.existed == ()
    for name in result.created:
        assert (tmp_path / name).is_file()


def test_initialize_is_idempotent(tmp_path):
    """Verify a second call creates nothing and reports everything as already present."""
    initialize(tmp_path)
    result = initialize(tmp_path)

    assert result.created == ()
    assert set(result.existed) == {
        "_system/vault.toml",
        ".gitignore",
        "README.md",
    }


def test_initialize_never_overwrites_a_hand_edited_readme(tmp_path):
    """Verify a curated README survives a second initialization."""
    initialize(tmp_path)
    hand_written = "# My Vault\n\nHand-written notes about this vault.\n"
    (tmp_path / "README.md").write_text(hand_written, encoding="utf-8")

    initialize(tmp_path)

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == hand_written


def test_initialize_refuses_a_non_empty_directory_holding_no_marker(tmp_path):
    """Verify a directory with unrelated files is refused, naming --force in the message."""
    (tmp_path / "some-file.txt").write_text("x", encoding="utf-8")

    with pytest.raises(NotAVaultError, match="--force"):
        initialize(tmp_path)


def test_initialize_accepts_a_non_empty_directory_with_force(tmp_path):
    """Verify --force lets a non-vault directory become one."""
    (tmp_path / "some-file.txt").write_text("x", encoding="utf-8")

    result = initialize(tmp_path, force=True)

    assert "_system/vault.toml" in result.created


def test_initialize_accepts_an_already_initialized_directory_without_force(tmp_path):
    """Verify the idempotent case never needs --force, since the directory is a vault."""
    initialize(tmp_path)

    result = initialize(tmp_path)

    assert result.created == ()


def test_initialize_ignores_dot_entries_when_checking_emptiness(tmp_path):
    """Verify a directory holding only .git counts as empty.

    `git init` followed by `sessionmemory init` is the ordinary sequence, so a bare .git
    directory must not trigger the non-empty refusal.
    """
    (tmp_path / ".git").mkdir()

    result = initialize(tmp_path)

    assert "_system/vault.toml" in result.created


def test_initialize_ignores_sqlite_indexes(tmp_path):
    """Verify a fresh vault's gitignore keeps every field's index out of git."""
    initialize(tmp_path)
    assert "*.sqlite3" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


def test_readme_explains_the_files_a_reader_finds_beside_the_pages(tmp_path):
    """Verify the vault README names the index file, the registry, and the CLI command."""
    initialize(tmp_path)

    text = (tmp_path / "README.md").read_text(encoding="utf-8")

    assert f"`{MODEL_CODE}.sqlite3`" in text
    assert "`_system/registry.toml`" in text
    assert "`sessionmemory new learning`" in text
    assert "`backlog.md` is the list of open items" in text
