"""Git helpers shared by the git-backed unit tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    """Run git in `cwd` and return its stripped stdout."""
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def commit(cwd: Path, name: str) -> str:
    """Commit a new file called `name` and return the commit sha."""
    (cwd / f"{name}.txt").write_text(name, encoding="utf-8")
    git(cwd, "add", ".")
    git(cwd, "commit", "-q", "-m", name)
    return git(cwd, "rev-parse", "HEAD")


@dataclass(frozen=True)
class Fixture:
    """A clone with a pushed trunk, a live branch, and a deleted branch."""

    clone: Path
    main_head: str
    feat: str
    gone: str


def rebase_merge(clone: Path) -> tuple[list[str], list[str]]:
    """Rebase-merge a three-commit branch onto trunk, as GitHub's rebase merge does.

    Trunk moves first so each copy gets a new parent and so a new sha. The branch is then
    deleted, leaving the originals as objects no ref holds.

    Returns:
        tuple[list[str], list[str]]: The original commits and their copies on trunk, in
            order. The last copy is what GitHub reports as the merge commit.
    """
    git(clone, "checkout", "-q", "-b", "pr", "main")
    originals = [commit(clone, name) for name in ("ra", "rb", "rc")]
    git(clone, "checkout", "-q", "main")
    commit(clone, "trunk-moves")
    for sha in originals:
        git(clone, "cherry-pick", sha)
    copies = git(clone, "rev-list", "--reverse", "-n", "3", "HEAD").split()
    git(clone, "push", "-q", "origin", "main")
    git(clone, "branch", "-q", "-D", "pr")
    git(clone, "reflog", "expire", "--expire=now", "--all")
    return originals, copies


def squash_merge(clone: Path) -> tuple[list[str], str]:
    """Squash-merge a two-commit branch onto trunk and delete the branch.

    Returns:
        tuple[list[str], str]: The original commits and the squash commit on trunk.
    """
    git(clone, "checkout", "-q", "-b", "pr", "main")
    originals = [commit(clone, name) for name in ("sa", "sb")]
    git(clone, "checkout", "-q", "main")
    git(clone, "merge", "-q", "--squash", "pr")
    git(clone, "commit", "-q", "-m", "squashed pr")
    squash = git(clone, "rev-parse", "HEAD")
    git(clone, "push", "-q", "origin", "main")
    git(clone, "branch", "-q", "-D", "pr")
    git(clone, "reflog", "expire", "--expire=now", "--all")
    return originals, squash
