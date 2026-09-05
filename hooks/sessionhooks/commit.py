"""Commit the vault's outstanding changes from a hook.

Replaces the scheduled checkpoint job. SessionStart and SessionEnd call this, and so
does the sweep worker once its writes are validated, so a page lands in git within the
session that produced it. Two hooks committing at once race on git's own index lock;
the loser returns None and the next call commits what it left behind. A merge, rebase,
cherry-pick, revert, or detached HEAD is skipped as well.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sessionhooks.store import git_safe_env

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

# A commit landing during any of these bakes half-resolved state into history.
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


def _git(
    root: Path, *args: str, env: Mapping[str, str], timeout: int
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=git_safe_env(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def is_repository(root: Path, *, env: Mapping[str, str], timeout: int = 10) -> bool:
    """Report whether `root` is inside a git work tree."""
    proc = _git(root, "rev-parse", "--is-inside-work-tree", env=env, timeout=timeout)
    return proc is not None and proc.returncode == 0 and proc.stdout.strip() == "true"


def is_safe_to_commit(root: Path, *, env: Mapping[str, str], timeout: int = 10) -> bool:
    """Report whether a commit now would land on a branch with no operation in flight."""
    git_dir = _git(root, "rev-parse", "--git-dir", env=env, timeout=timeout)
    if git_dir is None or git_dir.returncode != 0:
        return False
    marker_root = root / git_dir.stdout.strip()
    if any((marker_root / name).exists() for name in _IN_PROGRESS):
        return False
    head = _git(root, "symbolic-ref", "-q", "HEAD", env=env, timeout=timeout)
    return head is not None and head.returncode == 0


def commit_vault(root: Path, *, env: Mapping[str, str], timeout: int = 10) -> str | None:
    """Stage and commit everything under `root`; return the short sha, or None.

    None means nothing to commit, not a repository, or a git failure. A failed
    `git add` is not committed over: that would write a commit silently missing the
    one file that needed attention.
    """
    if not is_repository(root, env=env, timeout=timeout):
        return None
    if not is_safe_to_commit(root, env=env, timeout=timeout):
        return None
    status = _git(root, "status", "--porcelain", env=env, timeout=timeout)
    if status is None or status.returncode != 0 or not status.stdout.strip():
        return None
    # The pathspec excludes the derived index rather than trusting .gitignore: a
    # vault's ignore file may predate the index, since `sessionmemory init` never
    # overwrites an existing one.
    added = _git(
        root, "add", "-A", "--", ":/", ":(exclude,glob)**/*.sqlite3*", env=env, timeout=timeout
    )
    if added is None or added.returncode != 0:
        return None
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    committed = _git(root, "commit", "-q", "-m", f"checkpoint {stamp}", env=env, timeout=timeout)
    if committed is None or committed.returncode != 0:
        return None
    head = _git(root, "rev-parse", "--short", "HEAD", env=env, timeout=timeout)
    return head.stdout.strip() if head is not None and head.returncode == 0 else None
