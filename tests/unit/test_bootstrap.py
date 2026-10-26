"""Tests for vault initialization.

`initialize` creates the files a vault needs before anything can be written into it.
The tests here focus on the two properties that matter most: it never destroys a
hand-edited file, and its refusal to touch a non-empty non-vault directory is the whole
reason `require_vault` can trust `is_initialized` later.
"""

from __future__ import annotations

import subprocess

import pytest

from sessionmemory.lib import bootstrap
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
    assert "`sessionmemory new backlog`" in text
    assert "`backlog.md` is the list of open items" in text
    flat = " ".join(text.split())
    assert "every top-level bullet is an open item" in flat
    assert "`## decide`" in flat
    assert "`plans/` is scratch and is never committed" in flat
    assert "grouped by commit type" not in flat


def test_initialize_ignores_plans(tmp_path):
    """Verify a fresh vault's gitignore keeps every project's plans out of git."""
    initialize(tmp_path)
    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "projects/*/plans/" in lines


def test_ensure_local_ignore_appends_missing_lines_once(tmp_path):
    """Verify the local lines are appended once and a missing gitignore is created."""
    (tmp_path / ".gitignore").write_text("*.sqlite3\n", encoding="utf-8")

    assert bootstrap.ensure_local_ignore(tmp_path) is True
    first = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert first.endswith("\n")
    assert first.splitlines()[0] == "*.sqlite3"
    assert first.splitlines()[-len(bootstrap.LOCAL_IGNORE_LINES) :] == list(
        bootstrap.LOCAL_IGNORE_LINES
    )

    assert bootstrap.ensure_local_ignore(tmp_path) is False
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == first

    bare = tmp_path / "bare"
    bare.mkdir()
    assert bootstrap.ensure_local_ignore(bare) is True
    bare_lines = (bare / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert bare_lines[-len(bootstrap.LOCAL_IGNORE_LINES) :] == list(bootstrap.LOCAL_IGNORE_LINES)


def test_gitignore_reincludes_local_over_global_excludes(tmp_path):
    """Verify the vault's gitignore overrides global rules for files under local/."""
    vault = tmp_path / "vault"
    initialize(vault)
    excludes = tmp_path / "global-ignore"
    excludes.write_text(
        "local/\nclaude/\nsuperpowers/\nenv/\n*.log\nCLAUDE.local.md\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", "."], cwd=vault, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "core.excludesFile", str(excludes)],
        cwd=vault,
        check=True,
        capture_output=True,
    )
    local = vault / "projects" / "p" / "local"
    wanted = [
        "CLAUDE.local.md",
        "claude/skills/s/SKILL.md",
        "claude/skills/s/superpowers/x.md",
        "claude/skills/s/env/y.txt",
        "claude/rules/debug.log",
    ]
    debris = [
        ".DS_Store",
        "claude/skills/s/__pycache__/m.pyc",
        "claude/skills/s/node_modules/pkg/index.js",
        "claude/skills/s/.venv/bin/python",
        "claude/rules/r/venv/bin/python",
    ]
    for name in [*wanted, *debris]:
        target = local / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

    subprocess.run(["git", "add", "-A"], cwd=vault, check=True, capture_output=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=vault,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()

    prefix = "projects/p/local/"
    assert {name for name in staged if name.startswith(prefix)} == {prefix + n for n in wanted}


def test_ensure_local_ignore_writes_the_fresh_vault_comment(tmp_path):
    """Verify a migrated vault's gitignore carries the same local/ comment as a fresh one."""
    initialize(tmp_path / "fresh")
    fresh = (tmp_path / "fresh" / ".gitignore").read_text(encoding="utf-8")
    bootstrap.ensure_local_ignore(tmp_path)
    migrated = (tmp_path / ".gitignore").read_text(encoding="utf-8")

    block = "\n".join(migrated.splitlines()[: -len(bootstrap.LOCAL_IGNORE_LINES)])
    assert block
    assert block in fresh


def test_local_ignore_lines_reinclude_the_folder_before_its_contents():
    """Verify the folder itself is re-included first, since git cannot re-include under it."""
    assert bootstrap.LOCAL_IGNORE_LINES[:2] == ("!projects/*/local/", "!projects/*/local/**")


def test_ensure_local_ignore_adds_the_folder_line_to_an_older_vault(tmp_path):
    """Verify a vault holding the older lines gains only the missing ones."""
    older = (
        "!projects/*/local/**\nprojects/*/local/**/.DS_Store\nprojects/*/local/**/__pycache__/\n"
    )
    (tmp_path / ".gitignore").write_text(older, encoding="utf-8")

    assert bootstrap.ensure_local_ignore(tmp_path) is True

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert set(bootstrap.LOCAL_IGNORE_LINES) <= set(lines)
    assert lines.count("!projects/*/local/**") == 1


def test_ensure_local_ignore_repeats_exclusions_after_a_missing_reinclude(tmp_path):
    """Verify the cache exclusions follow an appended re-include, so they still win."""
    (tmp_path / ".gitignore").write_text(
        "\n".join(line for line in bootstrap.LOCAL_IGNORE_LINES if line != "!projects/*/local/**")
        + "\n",
        encoding="utf-8",
    )

    assert bootstrap.ensure_local_ignore(tmp_path) is True

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[-len(bootstrap.LOCAL_IGNORE_LINES) + 1 :] == list(bootstrap.LOCAL_IGNORE_LINES[1:])
