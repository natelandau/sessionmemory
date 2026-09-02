"""Answer "which project am I in".

Three keys are tried in order and the first hit wins: the normalized git remote, the
repository root, then a longest-prefix match of `cwd` against every registered root.

The third key applies only where git has positively ruled out a repository. A git
repository is its own project boundary, so an unregistered one is unregistered rather
than absorbed by whichever directory happens to contain it. Letting the path walk run
inside a repository would file a fresh checkout's notes under its parent directory's
project with nothing reported, and a slug is permanent once notes carry it. The same
reasoning bars the walk when git could not answer, since an unanswered question is not
an answer of "no repository", and when the repository is bare, since it has no working
tree for a working directory to belong to.

A worktree always resolves to its parent project, because the repository root comes from
`--git-common-dir`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessionmemory.lib import registry
from sessionmemory.lib.gitinfo import git_context

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class Resolution:
    """Which project a working directory belongs to."""

    slug: str | None
    registered: bool
    repo_root: Path | None
    is_worktree: bool


def resolve(vault: Path, cwd: Path) -> Resolution:
    """Map a working directory to a registered project.

    Args:
        vault (Path): The vault root.
        cwd (Path): The directory to resolve.

    Returns:
        Resolution: The match, or an unregistered result carrying what git did know.
    """
    context = git_context(cwd)
    projects = registry.load(vault)

    found = registry.find_by_remote(projects, context.remotes)

    if found is None and context.repo_root is not None:
        found = registry.find_by_root(projects, str(context.repo_root))

    if found is None and context.repo_root is None and context.git_answered and not context.is_bare:
        found = registry.find_by_path_prefix(projects, str(cwd.resolve()))

    if found is None:
        return Resolution(
            slug=None,
            registered=False,
            repo_root=context.repo_root,
            is_worktree=context.is_worktree,
        )

    return Resolution(
        slug=found.slug,
        registered=True,
        repo_root=context.repo_root,
        is_worktree=context.is_worktree,
    )
