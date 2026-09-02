"""Tests for committing the vault from a hook."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from sessionhooks import commit as commit_module  # ty: ignore[unresolved-import]
from sessionhooks.commit import (  # ty: ignore[unresolved-import]
    commit_vault,
    is_repository,
    is_safe_to_commit,
)

from tests._env import clean_environ

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path) -> Path:
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "t@example.org", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    return root


def test_commit_vault_commits_a_dirty_tree(tmp_path):
    """Verify a new file is committed and the short sha comes back."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")

    sha = commit_vault(root, env=clean_environ())

    assert sha is not None
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert "chore(vault): checkpoint" in log.stdout.splitlines()[0]


def test_commit_vault_clean_tree_is_none(tmp_path):
    """Verify nothing is committed when nothing changed."""
    assert commit_vault(_repo(tmp_path), env=clean_environ()) is None


def test_commit_vault_excludes_the_derived_index(tmp_path):
    """Verify a project's .sqlite3 index never enters git, even with an empty .gitignore."""
    # Given a repo with an empty .gitignore, a page, and a derived index sitting beside it
    root = _repo(tmp_path)
    (root / ".gitignore").write_text("", encoding="utf-8")
    learnings = root / "projects" / "demo" / "learnings"
    learnings.mkdir(parents=True)
    (learnings / "page.md").write_text("x", encoding="utf-8")
    (learnings / "stub.sqlite3").write_text("not real sqlite bytes", encoding="utf-8")

    # When committing the vault
    commit_vault(root, env=clean_environ())

    # Then the page is tracked and the index is not
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    assert "projects/demo/learnings/page.md" in tracked
    assert "stub.sqlite3" not in tracked


def test_commit_vault_outside_a_repository_is_none(tmp_path):
    """Verify a plain directory is left alone."""
    assert commit_vault(tmp_path, env=clean_environ()) is None
    assert is_repository(tmp_path, env=clean_environ()) is False


def test_commit_vault_ignores_an_ambient_git_dir(tmp_path):
    """Verify a leaked GIT_DIR cannot redirect the commit at another repository."""
    root = _repo(tmp_path)
    other = _repo(tmp_path / "other")
    (root / "page.md").write_text("x", encoding="utf-8")
    env = {**clean_environ(), "GIT_DIR": str(other / ".git")}

    commit_vault(root, env=env)

    other_log = subprocess.run(
        ["git", "log", "--oneline"], cwd=other, capture_output=True, text=True, check=True
    )
    assert "checkpoint" not in other_log.stdout


def test_commit_vault_skips_a_merge_in_progress(tmp_path):
    """Verify no commit lands during a merge conflict."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    (root / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")

    assert commit_vault(root, env=clean_environ()) is None
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1
    assert (root / "page.md").read_text(encoding="utf-8") == "x"


def test_commit_vault_skips_a_rebase_in_progress(tmp_path):
    """Verify no commit lands during a rebase."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    (root / ".git" / "rebase-merge").mkdir()

    assert commit_vault(root, env=clean_environ()) is None
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1
    assert (root / "page.md").read_text(encoding="utf-8") == "x"


def test_commit_vault_skips_a_detached_head(tmp_path):
    """Verify no commit lands on a detached HEAD."""
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    _git("checkout", "-q", "--detach", cwd=root)

    assert commit_vault(root, env=clean_environ()) is None
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1
    assert (root / "page.md").read_text(encoding="utf-8") == "x"


def test_git_swallows_a_subprocess_failure(tmp_path, monkeypatch):
    """Verify a git invocation that cannot even run reads as None, not a raised error."""
    # Given a repo and subprocess.run replaced with one that always raises
    root = _repo(tmp_path)

    def _raise(*_args, **_kwargs) -> None:
        message = "git executable not found"
        raise OSError(message)

    monkeypatch.setattr(commit_module.subprocess, "run", _raise)

    # When running a git command through the low-level helper
    result = commit_module._git(root, "status", env=clean_environ(), timeout=5)

    # Then the failure reads as None rather than raising
    assert result is None


def test_is_safe_to_commit_outside_a_repository_is_false(tmp_path):
    """Verify a plain directory is never judged safe to commit."""
    # Given a plain directory that is not a git repository
    # When checking whether it is safe to commit
    # Then it is never judged safe
    assert is_safe_to_commit(tmp_path, env=clean_environ()) is False


def test_commit_vault_returns_none_when_add_fails(tmp_path, monkeypatch):
    """Verify a failed `git add` is not committed over, so a bad stage never lands silently."""
    # Given a dirty repo whose `git add` is rigged to fail
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    real_git = commit_module._git

    def _fake_git(root_, *args, env, timeout=10) -> subprocess.CompletedProcess | None:
        if args and args[0] == "add":
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")
        return real_git(root_, *args, env=env, timeout=timeout)

    monkeypatch.setattr(commit_module, "_git", _fake_git)

    # When committing the vault
    result = commit_vault(root, env=clean_environ())

    # Then nothing is committed and the repo's log gains no new entry
    assert result is None
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1


def test_commit_vault_returns_none_when_commit_fails(tmp_path, monkeypatch):
    """Verify a failed `git commit` reads as nothing committed, not a raised error."""
    # Given a dirty repo whose `git commit` is rigged to fail
    root = _repo(tmp_path)
    (root / "page.md").write_text("x", encoding="utf-8")
    real_git = commit_module._git

    def _fake_git(root_, *args, env, timeout=10) -> subprocess.CompletedProcess | None:
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")
        return real_git(root_, *args, env=env, timeout=timeout)

    monkeypatch.setattr(commit_module, "_git", _fake_git)

    # When committing the vault
    result = commit_vault(root, env=clean_environ())

    # Then nothing is committed and the repo's log gains no new entry
    assert result is None
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True, check=True
    )
    assert len(log.stdout.strip().split("\n")) == 1
