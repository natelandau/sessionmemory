"""Tests for mapping a working directory to a project slug."""

from __future__ import annotations

import subprocess

import pytest

from sessionmemory.lib.registry import Project, save
from sessionmemory.lib.resolve import resolve


def _run(*args: str, cwd) -> None:
    """Run a git command, failing the test on a non-zero exit."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _register(vault, root, remotes=("github.com/nate/gx",)) -> None:
    """Write a one-project registry into the vault."""
    save(
        vault,
        {"gx": Project(slug="gx", remotes=tuple(remotes), root=str(root))},
    )


def test_unregistered_directory(tmp_path):
    """Report unregistered rather than guessing a slug."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = resolve(vault, tmp_path)
    assert not result.registered
    assert result.slug is None


def test_match_by_remote(tmp_path, monkeypatch):
    """Match on the normalized remote before anything else."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _register(vault, root="/somewhere/else")

    monkeypatch.setattr(
        "sessionmemory.lib.resolve.git_context",
        lambda _cwd: _FakeContext(repo_root=tmp_path, remotes=("github.com/nate/gx",)),
    )
    result = resolve(vault, tmp_path)
    assert result.slug == "gx"
    assert result.registered


def test_match_by_repository_root_when_no_remote(tmp_path, monkeypatch):
    """Fall back to the repository root for a repository with no remote."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _register(vault, root=tmp_path, remotes=())

    monkeypatch.setattr(
        "sessionmemory.lib.resolve.git_context",
        lambda _cwd: _FakeContext(repo_root=tmp_path, remotes=()),
    )
    assert resolve(vault, tmp_path).slug == "gx"


def test_worktree_resolves_to_the_parent_project(tmp_path, monkeypatch):
    """Resolve a worktree to its parent project, with is_worktree reported."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _register(vault, root=tmp_path)

    monkeypatch.setattr(
        "sessionmemory.lib.resolve.git_context",
        lambda _cwd: _FakeContext(
            repo_root=tmp_path,
            remotes=("github.com/nate/gx",),
            worktree_root=tmp_path / "wt",
        ),
    )
    result = resolve(vault, tmp_path / "wt")
    assert result.slug == "gx"
    assert result.is_worktree


def test_subdirectory_of_a_registered_root_resolves(tmp_path, monkeypatch):
    """Resolve any directory beneath a registered root, not only the root itself."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "notes"
    _register(vault, root=root, remotes=())

    monkeypatch.setattr("sessionmemory.lib.resolve.git_context", lambda _cwd: _FakeContext())
    result = resolve(vault, root / "a" / "b" / "c")
    assert result.slug == "gx"
    assert result.registered


def test_the_deepest_registered_root_wins(tmp_path, monkeypatch):
    """Resolve a nested project as itself rather than as the project containing it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outer = tmp_path / "outer"
    inner = outer / "nested" / "inner"
    save(
        vault,
        {
            "outer": Project(slug="outer", remotes=(), root=str(outer)),
            "inner": Project(slug="inner", remotes=(), root=str(inner)),
        },
    )

    monkeypatch.setattr("sessionmemory.lib.resolve.git_context", lambda _cwd: _FakeContext())
    assert resolve(vault, inner / "deep").slug == "inner"
    assert resolve(vault, outer / "nested").slug == "outer"


def test_a_repository_never_falls_through_to_a_containing_root(tmp_path, monkeypatch):
    """Report unregistered inside a git repository instead of walking up to an ancestor.

    The prefix walk is for directories outside git. Applying it inside a repository would
    hand a fresh checkout its parent directory's slug with nothing reported.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    outer = tmp_path / "outer"
    _register(vault, root=outer, remotes=())

    monkeypatch.setattr(
        "sessionmemory.lib.resolve.git_context",
        lambda _cwd: _FakeContext(repo_root=outer / "repo", remotes=()),
    )
    result = resolve(vault, outer / "repo" / "src")
    assert not result.registered
    assert result.slug is None


def test_no_answer_from_git_does_not_fall_through_to_a_containing_root(tmp_path, monkeypatch):
    """Report unregistered when git could not answer, rather than walking up to an ancestor.

    Empty git facts mean "nothing is known" as often as they mean "no repository here", so
    the walk would absorb any repository git happened to refuse to inspect.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    outer = tmp_path / "outer"
    _register(vault, root=outer, remotes=())

    monkeypatch.setattr(
        "sessionmemory.lib.resolve.git_context",
        lambda _cwd: _FakeContext(git_answered=False),
    )
    result = resolve(vault, outer / "repo" / "src")
    assert not result.registered
    assert result.slug is None


def test_a_bare_repository_does_not_fall_through_to_a_containing_root(tmp_path, monkeypatch):
    """Report unregistered in a bare repository rather than absorbing it into its container.

    A repository is its own project boundary whether or not it has a working tree.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    outer = tmp_path / "outer"
    _register(vault, root=outer, remotes=())

    monkeypatch.setattr(
        "sessionmemory.lib.resolve.git_context",
        lambda _cwd: _FakeContext(is_bare=True),
    )
    result = resolve(vault, outer / "thing.git")
    assert not result.registered
    assert result.slug is None


def test_a_git_key_wins_over_a_containing_registered_root(tmp_path, monkeypatch):
    """Keep a registered repository resolving as itself when a registered root contains it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    outer = tmp_path / "outer"
    repo = outer / "repo"
    save(
        vault,
        {
            "outer": Project(slug="outer", remotes=(), root=str(outer)),
            "repo": Project(slug="repo", remotes=("github.com/nate/repo",), root=""),
        },
    )

    monkeypatch.setattr(
        "sessionmemory.lib.resolve.git_context",
        lambda _cwd: _FakeContext(repo_root=repo, remotes=("github.com/nate/repo",)),
    )
    assert resolve(vault, repo).slug == "repo"


def test_end_to_end_with_a_real_repository_and_a_symlinked_registry_root(tmp_path):
    """Resolve through real git and a real registry entry, closing the seam between them.

    `git_context` canonicalizes the repository root; `find_by_root` must canonicalize
    the stored root too, or a registry entry written through a symlinked path (as
    `/tmp` and `/var` are on macOS) would silently fail to match and report the
    project as unregistered. Neither `git_context` nor `find_by_root` is faked here,
    so this exercises the real seam between them.
    """
    real_root = tmp_path / "real"
    real_root.mkdir()
    _run("init", "-q", ".", cwd=real_root)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=real_root)

    link = tmp_path / "link"
    try:
        link.symlink_to(real_root)
    except OSError:
        pytest.skip("platform does not support symlinks")

    vault = tmp_path / "vault"
    vault.mkdir()
    # Register the uncanonicalized symlink path, not the real path git will report.
    _register(vault, root=link, remotes=())

    result = resolve(vault, link)
    assert result.slug == "gx"
    assert result.registered


class _FakeContext:
    """A stand-in for GitContext with controllable values."""

    def __init__(
        self,
        repo_root=None,
        remotes=(),
        worktree_root=None,
        *,
        git_answered=True,
        is_bare=False,
    ):
        self.repo_root = repo_root
        self.remotes = remotes
        self.worktree_root = worktree_root or repo_root
        self.git_answered = git_answered
        self.is_bare = is_bare

    @property
    def is_worktree(self) -> bool:
        """Report whether the working tree differs from the main checkout."""
        return self.repo_root != self.worktree_root
