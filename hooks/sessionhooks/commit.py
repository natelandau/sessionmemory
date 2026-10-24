"""Commit the vault's outstanding changes from a hook.

Replaces the scheduled checkpoint job. SessionStart and SessionEnd call this, and so
does the sweep worker once its writes are validated, so a page lands in git within the
session that produced it. The message is built from the staged changes: the subject
names the project and counts what changed, and the body lists every path. Two hooks
committing at once race on git's own index lock; the loser reports `index lock` and the
next call commits what it left behind. A merge, rebase, cherry-pick, revert, or detached
HEAD is skipped as well.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessionhooks import log, shape
from sessionhooks.store import git_safe_env

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_log = log.logger("commit")

# The commit landed; only its name could not be read back. A skip reason here
# would send a reader hunting for a failure that never happened.
SHA_UNAVAILABLE = "(sha unavailable)"

_SUBJECT_LIMIT = 70
_ELLIPSIS = "..."
_VERBS = {"A": "add", "D": "delete"}  # every other status reads as an edit
_NOUNS = {
    "learnings": "learning",
    "logs": "log",
    "specs": "spec",
    "plans": "plan",
    "runbooks": "runbook",
    "reference": "reference doc",
}
_VAULT_GROUP = "vault"
# Nouns read in this order within a verb, whatever order git lists the paths in.
_NOUN_ORDER = ("log", "learning", "backlog", "spec", "plan", "runbook", "reference doc", "file")

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
    root: Path, *args: str, env: Mapping[str, str], timeout: int, stdin: str | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        proc = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=str(root),
            capture_output=True,
            input=stdin,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=git_safe_env(env),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("git %s did not run: %s", args[0], exc)
        return None
    _log.debug("git %s exited %d", args[0], proc.returncode)
    return proc


def _work_tree_prefix(
    root: Path, *, env: Mapping[str, str], timeout: int
) -> tuple[bool, str | None]:
    """Report whether `root` is in a work tree, and its path from the top when git says.

    One call answers both, so the commit's git-call budget does not grow.
    """
    proc = _git(
        root, "rev-parse", "--is-inside-work-tree", "--show-prefix", env=env, timeout=timeout
    )
    if proc is None or proc.returncode != 0:
        return False, None
    inside, newline, rest = proc.stdout.partition("\n")
    if inside.strip() != "true":
        return False, None
    if not newline or not rest:
        return True, None
    return True, rest.split("\n", 1)[0]


def _plans_exclusion(prefix: str | None) -> str:
    """Return the pathspec keeping the vault's plans out, anchored at the vault when known.

    Anchoring keeps an outer repository's own `projects/*/plans` staged when the vault is
    nested inside it. Without a prefix, plans are excluded at any depth.
    """
    if prefix is None:
        return ":(exclude,glob)**/projects/*/plans/**"
    escaped = re.sub(r"([*?\[\\])", r"\\\1", prefix)
    return f":(top,exclude,glob){escaped}projects/*/plans/**"


def _local_md_paths(root: Path) -> list[str]:
    """List each project's regular-file CLAUDE.local.md, relative to `root`.

    A global gitignore commonly names the file, which hides it from `add -A` and from
    `status --porcelain`, so these are force-staged. A symlink is skipped, since a vault
    copy is a real file and a link at that path is not one.
    """
    return [
        path.relative_to(root).as_posix()
        for path in sorted((root / "projects").glob(f"*/{shape.LOCAL_MD}"))
        if path.is_file() and not path.is_symlink()
    ]


def _has_changes(status: str, local_md: list[str], *, prefix: str | None) -> bool:
    """Report whether a `status --porcelain -z --ignored` listing holds anything to commit.

    An ignored entry counts only when it is a CLAUDE.local.md the commit force-stages;
    every other ignored file stays out of history. Porcelain paths are relative to the
    repository's top, hence the vault's prefix.
    """
    forced = {f"{prefix or ''}{path}" for path in local_md}
    for entry in status.split("\0"):
        if not entry:
            continue
        if not entry.startswith("!! ") or entry[3:] in forced:
            return True
    return False


def _force_add(
    root: Path, paths: list[str], *, env: Mapping[str, str], timeout: int
) -> CommitOutcome | None:
    """Force-stage `paths` past any ignore rule, returning the failure or None on success."""
    if not paths:
        return None
    # `literal` keeps a project folder named with `[` or `*` from matching other paths.
    literal = [f":(literal){path}" for path in paths]
    proc = _git(root, "add", "-f", "--", *literal, env=env, timeout=timeout)
    if proc is None or proc.returncode != 0:
        return CommitOutcome(reason=_failure(proc, "add"))
    return None


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


def _at_segment(parts: list[str]) -> tuple[str, str] | None:
    """Read a group from path segments that start at the vault's top, or None when they do not."""
    if parts[0] == "projects" and len(parts) > 1:
        return parts[1], "/".join(parts[2:])
    if parts[0] == "_system":
        return _VAULT_GROUP, "/".join(parts[1:])
    return None


def _locate(path: str, prefix: str | None = None) -> tuple[str, str]:
    """Split a repo-relative path into its group and the part below the group.

    `prefix` is the vault's own path inside the repository, as `--show-prefix` reports
    it, and the group is read from the segment right after it, since an outer folder
    may itself be named `projects`. A path outside the prefix belongs to no project.
    Without a prefix the first `projects/` or `_system/` segment is located instead.
    """
    if prefix is not None:
        if not path.startswith(prefix):
            return _VAULT_GROUP, path
        return _at_segment(path[len(prefix) :].split("/")) or (_VAULT_GROUP, path)
    parts = path.split("/")
    for index in range(len(parts)):
        located = _at_segment(parts[index:])
        if located is not None:
            return located
    return _VAULT_GROUP, path


def _noun(rest: str) -> str:
    """The singular noun for a path below its project."""
    if rest == "backlog.md":
        return "backlog"
    folder, sep, _ = rest.partition("/")
    return _NOUNS.get(folder, "file") if sep else "file"


def _truncate(subject: str) -> str:
    """Cut an over-long subject at the last separator that fits and end it with `...`."""
    if len(subject) <= _SUBJECT_LIMIT:
        return subject
    room = _SUBJECT_LIMIT - len(_ELLIPSIS)
    cut = max(subject.rfind(", ", 0, room + 1), subject.rfind("; ", 0, room + 1))
    return f"{subject[: cut if cut > 0 else room]}{_ELLIPSIS}"


def _clause(verb: str, rests: list[str]) -> str:
    """One verb's clause: `add 1 log, 2 learnings`, with the backlog named and never counted."""
    counts: dict[str, int] = {}
    for rest in rests:
        noun = _noun(rest)
        counts[noun] = counts.get(noun, 0) + 1
    items = [
        noun if noun == "backlog" else f"{count} {noun if count == 1 else noun + 's'}"
        for noun, count in sorted(counts.items(), key=lambda item: _NOUN_ORDER.index(item[0]))
    ]
    return f"{verb} {', '.join(items)}"


def message(
    changes: list[tuple[str, str]], *, note: str = "", prefix: str | None = None
) -> tuple[str, str]:
    """Build a commit's `(subject, body)` from `(status, path)` pairs.

    The subject names the project and counts what changed. When several groups
    changed it only counts files, since a per-kind breakdown would not fit. `prefix`
    is the vault's path inside the repository, which anchors the project segment.
    """
    verbed = [
        (_VERBS.get(status, "edit"), *_locate(path, prefix), path) for status, path in changes
    ]
    groups = sorted({group for _, group, _, _ in verbed})
    # `vault` is not a project: beside a real project its paths live only in the body.
    projects = [group for group in groups if group != _VAULT_GROUP] or groups
    if len(projects) > 1:
        count = len(verbed)
        subject = f"{', '.join(projects)}: update {count} file{'' if count == 1 else 's'}"
    else:
        scoped = [entry for entry in verbed if entry[1] == projects[0]]
        clauses = [
            _clause(verb, [rest for v, _, rest, _ in scoped if v == verb])
            for verb in ("add", "edit", "delete")
            if any(v == verb for v, _, _, _ in scoped)
        ]
        subject = f"{projects[0]}: {'; '.join(clauses)}"
    lines = [f"{verb}: {path}" for verb, _, _, path in verbed]
    body = "\n".join([note, "", *lines] if note else lines)
    return _truncate(subject), body


def _staged(root: Path, *, env: Mapping[str, str], timeout: int) -> list[tuple[str, str]] | None:
    """The staged `(status, path)` pairs, with paths relative to the repository top."""
    proc = _git(
        root, "diff", "--cached", "--name-status", "-z", "--no-renames", env=env, timeout=timeout
    )
    if proc is None or proc.returncode != 0:
        return None
    fields = proc.stdout.split("\0")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields) - 1, 2)]


def _failure(proc: subprocess.CompletedProcess[str] | None, step: str) -> str:
    """Name why a git step failed: a lost race on git's own lock, or the step itself."""
    if proc is not None and "index.lock" in (proc.stderr or ""):
        return "index lock"
    return f"{step} failed"


def _stage_all(
    root: Path, prefix: str | None, local_md: list[str], *, env: Mapping[str, str], timeout: int
) -> CommitOutcome | None:
    """Stage the vault's changes, returning the failure or None on success."""
    # The pathspec excludes the derived index and plans rather than trusting
    # .gitignore: a vault's ignore file may predate either rule, since
    # `sessionmemory init` never overwrites an existing one.
    added = _git(
        root,
        "add",
        "-A",
        "--",
        ":/",
        ":(exclude,glob)**/*.sqlite3*",
        _plans_exclusion(prefix),
        env=env,
        timeout=timeout,
    )
    if added is None or added.returncode != 0:
        return CommitOutcome(reason=_failure(added, "add"))
    return _force_add(root, local_md, env=env, timeout=timeout)


def commit_vault(  # noqa: PLR0911 - each early return names a distinct reason nothing landed
    root: Path, *, env: Mapping[str, str], timeout: int = 10, note: str = ""
) -> CommitOutcome:
    """Stage and commit everything under `root`, reporting the sha or why nothing landed.

    A failed `git add` is not committed over: that would write a commit silently
    missing the one file that needed attention. `note` opens the commit body.
    """
    inside, prefix = _work_tree_prefix(root, env=env, timeout=timeout)
    if not inside:
        return CommitOutcome(reason="not a repository")
    unsafe = _unsafe_reason(root, env=env, timeout=timeout)
    if unsafe is not None:
        return CommitOutcome(reason=unsafe)
    # `--ignored` because a global ignore rule hides an untracked CLAUDE.local.md from a
    # plain status, and this one call keeps the commit's git-call budget unchanged.
    status = _git(
        root, "status", "--porcelain", "-z", "--ignored=matching", env=env, timeout=timeout
    )
    if status is None or status.returncode != 0:
        return CommitOutcome(reason="git unavailable")
    local_md = _local_md_paths(root)
    if not _has_changes(status.stdout, local_md, prefix=prefix):
        return CommitOutcome(reason="clean")
    failure = _stage_all(root, prefix, local_md, env=env, timeout=timeout)
    if failure is not None:
        return failure
    staged = _staged(root, env=env, timeout=timeout)
    if staged is None:
        return CommitOutcome(reason="git unavailable")
    if not staged:
        return CommitOutcome(reason="clean")
    subject, body = message(staged, note=note, prefix=prefix)
    # Via stdin: a large first commit's body would exceed the per-argument limit.
    committed = _git(
        root, "commit", "-q", "-F", "-", env=env, timeout=timeout, stdin=f"{subject}\n\n{body}\n"
    )
    if committed is None or committed.returncode != 0:
        return CommitOutcome(reason=_failure(committed, "commit"))
    head = _git(root, "rev-parse", "--short", "HEAD", env=env, timeout=timeout)
    if head is None or head.returncode != 0:
        return CommitOutcome(sha=SHA_UNAVAILABLE)
    return CommitOutcome(sha=head.stdout.strip())
