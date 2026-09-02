"""Tests for reading git context from a working directory."""

from __future__ import annotations

import subprocess

import pytest

from sessionmemory.lib.gitinfo import git_context

REMOTE = "ssh://git@gitea.example.org:2222/nate/demo.git"


def _run(*args: str, cwd) -> None:
    """Run a git command, failing the test on a non-zero exit."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """Build a git repository with one commit and one remote."""
    root = tmp_path / "main"
    root.mkdir()
    _run("init", "-q", ".", cwd=root)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    _run("remote", "add", "origin", REMOTE, cwd=root)
    return root


def test_context_in_a_plain_checkout(repo):
    """Report the repository root, no worktree, and the remote."""
    ctx = git_context(repo)
    assert ctx.repo_root == repo
    assert not ctx.is_worktree
    assert "gitea.example.org/nate/demo" in ctx.remotes


def test_context_from_a_subdirectory(repo):
    """Report the repository root when called from a nested directory."""
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert git_context(nested).repo_root == repo


def test_worktree_reports_the_parent_repository_root(repo, tmp_path):
    """Resolve a worktree to the main checkout, not to itself.

    This is the regression guard for the failure mode that would give every feature
    branch its own project slug.
    """
    worktree = tmp_path / "feature"
    _run("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=repo)

    ctx = git_context(worktree)
    assert ctx.repo_root == repo
    assert ctx.worktree_root == worktree
    assert ctx.is_worktree
    assert "gitea.example.org/nate/demo" in ctx.remotes


def test_a_submodule_reports_its_own_working_tree(repo, tmp_path):
    """Report a submodule's checkout, not a directory inside the superproject's `.git`.

    A submodule's `--git-common-dir` is `<super>/.git/modules/<path>`, so the
    parent-of-common-dir rule would name `<super>/.git/modules`, which is no working tree
    and which every submodule under one directory would share.
    """
    superproject = tmp_path / "super"
    superproject.mkdir()
    _run("init", "-q", ".", cwd=superproject)
    _run("commit", "-q", "--allow-empty", "-m", "init", cwd=superproject)
    _run(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(repo),
        "vendor/sub",
        cwd=superproject,
    )

    ctx = git_context(superproject / "vendor" / "sub")
    assert ctx.repo_root == superproject / "vendor" / "sub"
    assert not ctx.is_worktree


def test_a_separate_git_dir_reports_the_working_tree(tmp_path):
    """Report the checkout, not its parent, when the git directory lives elsewhere.

    `--git-common-dir` is the given directory here, so the parent-of-common-dir rule would
    hand the directory containing it, and every path beneath that, this project's slug.
    """
    work = tmp_path / "work"
    _run("init", "-q", f"--separate-git-dir={tmp_path / 'elsewhere.git'}", str(work), cwd=tmp_path)

    ctx = git_context(work)
    assert ctx.repo_root == work
    assert not ctx.is_worktree


def test_remotes_come_only_from_this_repository(repo, tmp_path, monkeypatch):
    """Ignore a remote configured outside this repository.

    A remote in the user's own git config is not this repository's remote, and treating
    it as one would key every repository on the machine to the same registry entry.
    """
    global_config = tmp_path / "gitconfig"
    global_config.write_text(
        '[remote "stray"]\n\turl = ssh://git@example.com/someone/stray.git\n', encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    remotes = git_context(repo).remotes
    assert remotes == ("gitea.example.org/nate/demo",)


def test_context_outside_a_repository(tmp_path):
    """Report no repository, and that git answered, when the directory is not under git."""
    ctx = git_context(tmp_path)
    assert ctx.repo_root is None
    assert ctx.remotes == ()
    assert not ctx.is_worktree
    assert ctx.git_answered


def test_a_bare_repository_reports_no_working_tree(tmp_path):
    """Report a bare repository as having no roots, not as the directory containing it.

    `--git-common-dir` in a bare repository is the repository directory itself, so the
    parent-of-common-dir rule would name its container and hand that container's whole
    subtree a project slug.
    """
    container = tmp_path / "bareparent"
    container.mkdir()
    bare = container / "thing.git"
    _run("init", "--bare", "-q", str(bare), cwd=container)

    ctx = git_context(bare)
    assert ctx.is_bare
    assert ctx.git_answered
    assert ctx.repo_root is None
    assert ctx.worktree_root is None
    assert not ctx.is_worktree


def test_a_main_checkout_is_not_bare(repo):
    """Keep an ordinary checkout out of the bare path."""
    assert not git_context(repo).is_bare


def test_a_worktree_is_not_bare(repo, tmp_path):
    """Keep a linked worktree out of the bare path, since it has a working tree of its own."""
    worktree = tmp_path / "feature"
    _run("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=repo)

    ctx = git_context(worktree)
    assert not ctx.is_bare
    assert ctx.repo_root == repo


def test_missing_git_binary_reports_that_git_did_not_answer(repo, monkeypatch):
    """Report no repository and no answer, not a crash, when the git executable is absent.

    The empty roots alone would be indistinguishable from a directory outside git, which
    is a real repository being reported as a plain directory.
    """

    def _missing(*_args, **_kwargs) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", _missing)
    ctx = git_context(repo)
    assert ctx.repo_root is None
    assert ctx.remotes == ()
    assert not ctx.is_worktree
    assert not ctx.git_answered


def test_a_timeout_reports_that_git_did_not_answer(repo, monkeypatch):
    """Treat a hung git as an open question rather than as an absent repository."""

    def _hang(*_args, **_kwargs) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", _hang)
    ctx = git_context(repo)
    assert ctx.repo_root is None
    assert not ctx.git_answered


def test_a_refused_directory_reports_that_git_did_not_answer(repo, monkeypatch):
    """Treat any other non-zero exit, such as a safe.directory refusal, as no answer."""

    def _refuse(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=128,
            stdout="",
            stderr=f"fatal: detected dubious ownership in repository at '{repo}'\n",
        )

    monkeypatch.setattr(subprocess, "run", _refuse)
    ctx = git_context(repo)
    assert ctx.repo_root is None
    assert not ctx.git_answered
