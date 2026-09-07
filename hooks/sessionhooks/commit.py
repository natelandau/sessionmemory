"""Commit the vault's outstanding changes from a hook.

Replaces the scheduled checkpoint job. SessionStart and SessionEnd call this, and so
does the sweep worker once its writes are validated, so a page lands in git within the
session that produced it. Two hooks committing at once race on git's own index lock;
the loser reports `index lock` and the next call commits what it left behind. A merge,
rebase, cherry-pick, revert, or detached HEAD is skipped as well.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sessionhooks import log
from sessionhooks.store import git_safe_env

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_log = log.logger("commit")

# The commit landed; only its name could not be read back. A skip reason here
# would send a reader hunting for a failure that never happened.
SHA_UNAVAILABLE = "(sha unavailable)"

# A commit landing during any of these bakes half-resolved state into history.
_IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    """What a commit attempt did: a short sha, or the reason nothing was committed."""

    sha: str | None = None
    reason: str = ""

    def describe(self) -> str:
        """The outcome as one log-line clause."""
        return f"committed {self.sha}" if self.sha else f"commit skipped: {self.reason}"


def _git(
    root: Path, *args: str, env: Mapping[str, str], timeout: int
) -> subprocess.CompletedProcess[str] | None:
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=git_safe_env(env),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("git %s did not run: %s", args[0], exc)
        return None
    _log.debug("git %s exited %d", args[0], proc.returncode)
    return proc


def is_repository(root: Path, *, env: Mapping[str, str], timeout: int = 10) -> bool:
    """Report whether `root` is inside a git work tree."""
    proc = _git(root, "rev-parse", "--is-inside-work-tree", env=env, timeout=timeout)
    return proc is not None and proc.returncode == 0 and proc.stdout.strip() == "true"


def _unsafe_reason(root: Path, *, env: Mapping[str, str], timeout: int) -> str | None:
    """Why a commit now would land on half-resolved state, or None when it is safe."""
    git_dir = _git(root, "rev-parse", "--git-dir", env=env, timeout=timeout)
    if git_dir is None or git_dir.returncode != 0:
        return "not a repository"
    marker_root = root / git_dir.stdout.strip()
    if any((marker_root / name).exists() for name in _IN_PROGRESS):
        return "operation in progress"
    head = _git(root, "symbolic-ref", "-q", "HEAD", env=env, timeout=timeout)
    if head is None or head.returncode != 0:
        return "detached HEAD"
    return None


def is_safe_to_commit(root: Path, *, env: Mapping[str, str], timeout: int = 10) -> bool:
    """Report whether a commit now would land on a branch with no operation in flight."""
    return _unsafe_reason(root, env=env, timeout=timeout) is None


def _failure(proc: subprocess.CompletedProcess[str] | None, step: str) -> str:
    """Name why a git step failed: a lost race on git's own lock, or the step itself."""
    if proc is not None and "index.lock" in (proc.stderr or ""):
        return "index lock"
    return f"{step} failed"


def commit_vault(  # noqa: PLR0911 - each early return names a distinct reason nothing landed
    root: Path, *, env: Mapping[str, str], timeout: int = 10
) -> CommitOutcome:
    """Stage and commit everything under `root`, reporting the sha or why nothing landed.

    A failed `git add` is not committed over: that would write a commit silently
    missing the one file that needed attention.
    """
    if not is_repository(root, env=env, timeout=timeout):
        return CommitOutcome(reason="not a repository")
    unsafe = _unsafe_reason(root, env=env, timeout=timeout)
    if unsafe is not None:
        return CommitOutcome(reason=unsafe)
    status = _git(root, "status", "--porcelain", env=env, timeout=timeout)
    if status is None or status.returncode != 0:
        return CommitOutcome(reason="git unavailable")
    if not status.stdout.strip():
        return CommitOutcome(reason="clean")
    # The pathspec excludes the derived index rather than trusting .gitignore: a
    # vault's ignore file may predate the index, since `sessionmemory init` never
    # overwrites an existing one.
    added = _git(
        root, "add", "-A", "--", ":/", ":(exclude,glob)**/*.sqlite3*", env=env, timeout=timeout
    )
    if added is None or added.returncode != 0:
        return CommitOutcome(reason=_failure(added, "add"))
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    committed = _git(root, "commit", "-q", "-m", f"checkpoint {stamp}", env=env, timeout=timeout)
    if committed is None or committed.returncode != 0:
        return CommitOutcome(reason=_failure(committed, "commit"))
    head = _git(root, "rev-parse", "--short", "HEAD", env=env, timeout=timeout)
    if head is None or head.returncode != 0:
        return CommitOutcome(sha=SHA_UNAVAILABLE)
    return CommitOutcome(sha=head.stdout.strip())
