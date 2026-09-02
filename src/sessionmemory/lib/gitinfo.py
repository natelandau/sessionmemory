"""Read the git facts that identify which project a directory belongs to.

The repository root is the main checkout's working tree, derived from
`--git-common-dir`, never from `--show-toplevel` alone. In a worktree the two differ:
`--show-toplevel` returns the worktree directory while `--git-common-dir` returns the
main checkout's `.git`. Resolving on the former would make every worktree an
unregistered project. `_main_worktree` documents the layouts where the shared git
directory is not a `.git` beside the checkout.

An empty answer from git is not a fact about the directory. Git declining to inspect a
directory, timing out, or not being installed at all is a different state from git
reporting that the directory is not in a repository, and `GitContext.git_answered`
carries that distinction so a caller never reads git's silence as "there is no
repository here".
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sessionmemory.lib import registry

GIT_TIMEOUT_SECONDS = 10

# The one git failure that is an answer rather than an outage. Every other non-zero exit,
# such as a `safe.directory` refusal or a held index lock, leaves the question open.
_NOT_A_REPOSITORY = "not a git repository"


@dataclass(frozen=True)
class _GitAnswer:
    """One git invocation's outcome, separating a definite answer from a failure to ask."""

    stdout: str | None
    answered: bool


@dataclass(frozen=True)
class GitContext:
    """What git knows about a working directory."""

    repo_root: Path | None
    worktree_root: Path | None
    remotes: tuple[str, ...]
    git_answered: bool
    is_bare: bool

    @property
    def is_worktree(self) -> bool:
        """Report whether this directory is a linked worktree.

        Returns:
            bool: True when the working tree differs from the main checkout.
        """
        if self.repo_root is None or self.worktree_root is None:
            return False
        return self.repo_root != self.worktree_root


def _git(cwd: Path, *args: str) -> _GitAnswer:
    """Run a git command and report both its output and whether git answered at all.

    Args:
        cwd (Path): The directory to run in.
        *args: Arguments passed to git.

    Returns:
        _GitAnswer: The stripped stdout, and whether git reached a verdict. A non-zero
            exit counts as a verdict only when git says the directory is not a
            repository.
    """
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            # The C locale keeps git's own messages in the wording the classifier reads.
            env={**os.environ, "LC_ALL": "C", "LANGUAGE": ""},
        )
    except (OSError, subprocess.SubprocessError):
        return _GitAnswer(stdout=None, answered=False)

    if completed.returncode != 0:
        return _GitAnswer(stdout=None, answered=_NOT_A_REPOSITORY in completed.stderr.lower())
    return _GitAnswer(stdout=completed.stdout.strip() or None, answered=True)


def _main_worktree(cwd: Path, common_dir: Path, worktree_root: Path | None) -> Path:
    """Return the main checkout's working tree directory.

    The parent of `--git-common-dir` is the main checkout only where the shared git
    directory is a `.git` inside that checkout. A submodule's is
    `<superproject>/.git/modules/<path>` and a `--separate-git-dir` checkout's is
    wherever it was put, so the parent rule would name a directory that is no working
    tree at all: two submodules under one directory even share the same parent, which
    would make the second one look like the first. Git sets `core.worktree` in exactly
    the layouts where the parent rule fails, and it names the main working tree directly.

    Args:
        cwd (Path): The directory being inspected.
        common_dir (Path): The absolute `--git-common-dir` for `cwd`.
        worktree_root (Path | None): The `--show-toplevel` for `cwd`, if git gave one.

    Returns:
        Path: The main checkout's root.
    """
    # core.worktree is stored relative to the shared git directory, which is the same
    # file for a linked worktree as for the checkout it was added from.
    configured = _git(cwd, "config", "--local", "--get", "core.worktree").stdout
    if configured:
        return (common_dir / configured).resolve()

    if common_dir.name == ".git":
        return common_dir.resolve().parent

    # No rule names the main checkout here, so this working tree stands in for it. It is
    # a real directory, which the parent of the git directory would not be, and remotes
    # are the primary registry key regardless.
    return worktree_root or common_dir.resolve().parent


def git_context(cwd: Path) -> GitContext:
    """Read the repository root, worktree root, and normalized remotes for `cwd`.

    Args:
        cwd (Path): The directory to inspect.

    Returns:
        GitContext: The git facts. Both roots are None when `cwd` is not in a repository,
            when the repository is bare, and when git could not answer, so a caller that
            cares which of those it is reads `git_answered` and `is_bare`.
    """
    common_dir = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common_dir.stdout is None:
        return GitContext(
            repo_root=None,
            worktree_root=None,
            remotes=(),
            git_answered=common_dir.answered,
            is_bare=False,
        )

    # A bare repository's --git-common-dir is the repository directory itself, so the
    # parent-of-common-dir rule would name the directory containing it and hand that
    # directory's whole subtree a project slug. It has no working tree, so no working
    # directory belongs to it.
    is_bare = _git(cwd, "rev-parse", "--is-bare-repository").stdout == "true"
    if is_bare:
        return GitContext(
            repo_root=None, worktree_root=None, remotes=(), git_answered=True, is_bare=True
        )

    toplevel = _git(cwd, "rev-parse", "--show-toplevel").stdout
    worktree_root = Path(toplevel).resolve() if toplevel else None
    repo_root = _main_worktree(cwd, Path(common_dir.stdout), worktree_root)

    # --local reads only this repository's config. Without it a stray remote in
    # ~/.gitconfig would be read as this repository's remote and become a registry key
    # every repository on the machine shares. A worktree reads the same local config as
    # its main checkout, so a worktree still resolves to its parent project.
    listed = _git(cwd, "config", "--local", "--get-regexp", r"^remote\..*\.url").stdout or ""
    urls = [line.split(" ", 1)[1] for line in listed.splitlines() if " " in line]
    remotes = tuple(dict.fromkeys(registry.normalize_remote(url) for url in urls if url))

    return GitContext(
        repo_root=repo_root,
        worktree_root=worktree_root,
        remotes=remotes,
        git_answered=True,
        is_bare=False,
    )
