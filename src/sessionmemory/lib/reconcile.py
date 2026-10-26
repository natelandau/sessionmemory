"""Reconcile a project's vault records with what its repository now says.

The log-commits reconciler checks every commit a session log lists under `## Changes`
and decides, one sha at a time:

1. The commit is reachable from trunk: `KEEP`.
2. Origin is on GitHub, so ask GitHub about the commit's pull requests:
   - merged into trunk and the merge commit is on trunk: `REPLACE` with the commit's
     own rebased copy when exactly one trunk commit below the merge commit carries the
     same patch, as after a rebase merge, else with the merge commit.
   - merged but the merge commit is not on trunk yet: `UNCHECKED`.
   - open: `IN_PROGRESS`.
   - GitHub could not answer: `UNCHECKED`.
   - no live pull request: go on to rule 3.
3. A live branch or tag holds the commit: `IN_PROGRESS`.
4. `git fetch origin` failed, so origin's trunk may hold the commit: `UNCHECKED`.
5. The clone is shallow, so its history cannot prove the commit gone: `UNCHECKED`.
6. Origin's fetch refspec is narrowed, as in a single-branch clone, so a branch this
   clone never fetches may hold the commit: `UNCHECKED`.
7. Otherwise the commit is gone: `DROP`.

The fetch runs only when some logged commit is not on the local trunk, and never when
there is no origin, so a repeat run needs no network.

Every log's decisions are made before any log is written, so a `CommitsError` leaves the
vault as it was. Each log is read again just before its write and the decisions applied to
that copy, since a sweep may rewrite the log while git and GitHub are being asked. A log
that is not valid UTF-8 or whose frontmatter does not parse is skipped, since writing it
back would mangle bytes outside the section; `doctor` reports it.

GitHub is asked before the branches because it keeps a head branch after a squash merge
unless auto-delete is on, and a fetch without `--prune` keeps the refs of deleted
branches, so a branch check alone would call a squash-merged commit "in progress"
forever.

Only the `## Changes` section is ever rewritten, and the log's `updated` field is left
alone, since the session's own record did not change. A later reconciler is a function
added to `RECONCILERS`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from typing import TYPE_CHECKING

from sessionmemory.lib import changes, field, paths
from sessionmemory.lib.commits import CommitsError, Expansion, PullState, Repository
from sessionmemory.lib.frontmatter import FrontmatterError, parse

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sessionmemory.lib.field import Page

FETCH_FAILED_NOTE = "git fetch origin failed; left commits off the local trunk unchecked"
SHALLOW_NOTE = "shallow clone"
NARROWED_NOTE = "narrowed fetch refspec"


class Action(StrEnum):
    """What to do with one logged commit."""

    KEEP = "keep"
    REPLACE = "replace"
    DROP = "drop"
    IN_PROGRESS = "in_progress"
    UNCHECKED = "unchecked"


@dataclass(frozen=True)
class Decision:
    """The verdict for one commit.

    Attributes:
        action (Action): What to do with the line.
        replacement (str): The `<sha> <subject>` line to write, for `REPLACE`.
        merge_sha (str): The full sha of the replacing commit, for `REPLACE`.
        reason (str): Why the commit could not be checked, for `UNCHECKED`.
    """

    action: Action
    replacement: str = ""
    merge_sha: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Context:
    """Where a project's vault records and repository live.

    Attributes:
        vault (Path): The vault root.
        slug (str): The project's slug.
        root (Path): The project's repository working tree.
    """

    vault: Path
    slug: str
    root: Path


@dataclass
class Report:
    """What one reconciler found, counted per logged line across all logs.

    Attributes:
        name (str): The reconciler's name.
        changed (list[Path]): Logs whose commit list changes, also on a dry run.
        replaced (int): Logged lines replaced by the commit that landed on trunk. Lines
            that collapse into one replacement each count.
        dropped (int): Lines removed because the commit is gone.
        in_progress (int): Lines left because a branch or open pull request holds them.
        unchecked (int): Lines left because the answer was unavailable.
        notes (list[str]): Distinct explanations the caller should show.
    """

    name: str
    changed: list[Path] = dataclass_field(default_factory=list)
    replaced: int = 0
    dropped: int = 0
    in_progress: int = 0
    unchecked: int = 0
    notes: list[str] = dataclass_field(default_factory=list)


class _Judge:
    """Decide commits for one run, asking git and GitHub at most once per sha."""

    def __init__(
        self,
        repository: Repository,
        trunk: set[str],
        *,
        fetch_failed: bool,
        shallow: bool,
        narrowed: bool,
    ) -> None:
        self._repository = repository
        self._trunk = trunk
        self._fetch_failed = fetch_failed
        self._shallow = shallow
        self._narrowed = narrowed
        self._decisions: dict[str, Decision] = {}
        self._described: dict[str, str | None] = {}
        self._slug: str | None = None
        self._slug_known = False

    def decide(self, sha: str, expansion: Expansion) -> Decision:
        """Return the decision for a logged sha.

        Args:
            sha (str): The sha as logged.
            expansion (Expansion): The result of expanding every logged sha.

        Raises:
            CommitsError: When git cannot answer.
        """
        if sha in expansion.ambiguous:
            return Decision(Action.UNCHECKED, reason="short sha is ambiguous")
        full = expansion.full.get(sha)
        if full is not None and full in self._trunk:
            return Decision(Action.KEEP)
        cached = self._decisions.get(sha)
        if cached is None:
            cached = self._decide_off_trunk(sha, full)
            self._decisions[sha] = cached
        return cached

    def _github_slug(self) -> str | None:
        if not self._slug_known:
            self._slug = self._repository.github_slug()
            self._slug_known = True
        return self._slug

    def _decide_off_trunk(self, sha: str, full: str | None) -> Decision:
        slug = self._github_slug()
        if slug is not None:
            answered = self._ask_github(full or sha, slug=slug, full=full)
            if answered is not None:
                return answered
        return self._decide_locally(full)

    def _ask_github(self, sha: str, *, slug: str, full: str | None) -> Decision | None:
        """Return GitHub's verdict, or None when it has no live pull request."""
        answer = self._repository.pulls(sha, slug=slug)
        if answer.state == PullState.MERGED:
            return self._landed(answer.merge_sha, full=full)
        if answer.state == PullState.OPEN:
            return Decision(Action.IN_PROGRESS)
        if answer.state == PullState.UNANSWERED:
            return Decision(Action.UNCHECKED, reason=answer.reason)
        return None

    def _decide_locally(self, full: str | None) -> Decision:
        """Decide from the refs alone, never dropping what this clone cannot vouch for."""
        if full is not None and self._repository.in_branch(full, off_trunk=True):
            return Decision(Action.IN_PROGRESS)
        if self._fetch_failed:
            return Decision(Action.UNCHECKED, reason=FETCH_FAILED_NOTE)
        if self._shallow:
            return Decision(Action.UNCHECKED, reason=SHALLOW_NOTE)
        if self._narrowed:
            return Decision(Action.UNCHECKED, reason=NARROWED_NOTE)
        return Decision(Action.DROP)

    def _landed(self, merge_sha: str, *, full: str | None) -> Decision:
        """Return the replacement for a commit whose pull request merged into trunk."""
        if merge_sha not in self._trunk:
            return Decision(Action.UNCHECKED, reason="merge commit not on trunk")
        landed = merge_sha
        if full is not None:
            # The copy only refines the merge commit, so a git failure here keeps it.
            with contextlib.suppress(CommitsError):
                landed = self._repository.rebased_copy(full, merge_sha) or merge_sha
        if landed not in self._described:
            self._described[landed] = self._repository.describe(landed)
        line = self._described[landed]
        if line is None:
            return Decision(Action.UNCHECKED, reason="merge commit could not be described")
        return Decision(Action.REPLACE, replacement=line, merge_sha=landed)


def log_commits(context: Context, *, dry_run: bool) -> Report:
    """Replace or drop commits in each log's `## Changes` that no longer exist.

    Args:
        context (Context): The project and its repository.
        dry_run (bool): Report what would change without writing.

    Returns:
        Report: Counts per logged line, the logs that change, and notes.

    Raises:
        CommitsError: When git cannot answer. The command turns this into a clean
            failure, since a partial answer would drop commits that still exist.
    """
    report = Report(name="log-commits")
    logs = [
        page
        for path in field.iter_pages(paths.logs_dir(context.vault, context.slug))
        if (page := _read_log(path)) is not None
    ]
    parsed = [(page, changes.parse(page.body)) for page in logs]
    logged = [line.sha for _, lines in parsed for line in lines]
    if not logged:
        return report

    repository = Repository(context.root)
    trunk, expansion, fetch_failed = _refresh(repository, logged)
    if fetch_failed:
        report.notes.append(FETCH_FAILED_NOTE)
    judge = _Judge(
        repository,
        trunk,
        fetch_failed=fetch_failed,
        shallow=repository.is_shallow(),
        # Without an origin there is no unfetched branch to hold the commit.
        narrowed=repository.origin_url() is not None and not repository.fetches_all_branches(),
    )

    rewrites: list[tuple[Page, dict[str, str | None]]] = []
    for page, lines in parsed:
        outcome = _decide_page(page, lines, judge=judge, expansion=expansion, report=report)
        if outcome is not None:
            rewrites.append((page, outcome))

    for page, outcome in rewrites:
        report.changed.append(page.path)
        if dry_run:
            continue
        current = _read_log(page.path)
        if current is None:
            continue
        new_body = changes.rewrite(current.body, outcome)
        if new_body != current.body:
            field.write_page(page.path, current.meta, new_body)
    return report


def _read_log(path: Path) -> Page | None:
    """Read a log strictly, or return None when writing it back would alter other bytes."""
    try:
        meta, body = parse(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, FrontmatterError):
        return None
    return field.Page(path=path, meta=meta, body=body)


def _refresh(repository: Repository, logged: list[str]) -> tuple[set[str], Expansion, bool]:
    """Return the trunk commits, the expanded shas, and whether a needed fetch failed.

    Raises:
        CommitsError: When git cannot answer.
    """
    trunk = repository.trunk_commits()
    expansion = repository.expand(logged)
    if not _needs_fetch(logged, trunk=trunk, expansion=expansion) or not repository.origin_url():
        return trunk, expansion, False
    if not repository.fetch():
        return trunk, expansion, True
    return repository.trunk_commits(), repository.expand(logged), False


def _decide_page(
    page: Page,
    lines: list[changes.ChangeLine],
    *,
    judge: _Judge,
    expansion: Expansion,
    report: Report,
) -> dict[str, str | None] | None:
    """Decide one log's lines, tally them, and return the rewrite, or None when unchanged.

    The rewrite maps a logged sha to its replacement line, or to None to drop it, in the
    shape `changes.rewrite` takes.

    Raises:
        CommitsError: When git cannot answer.
    """
    decisions = [(line, judge.decide(line.sha, expansion)) for line in lines]
    if all(decision.action == Action.KEEP for _, decision in decisions):
        return None
    # A replacement whose commit is already listed under a sha of another length reuses
    # that line's text, so `changes.rewrite` sees one sha and lists it once.
    listed = {
        expansion.full[line.sha]: f"{line.sha} {line.subject}"
        for line in lines
        if line.sha in expansion.full
    }
    outcome: dict[str, str | None] = {}
    for line, decision in decisions:
        _tally(report, decision)
        if decision.action == Action.REPLACE:
            outcome[line.sha] = listed.get(decision.merge_sha, decision.replacement)
        elif decision.action == Action.DROP:
            outcome[line.sha] = None
    return None if changes.rewrite(page.body, outcome) == page.body else outcome


def _needs_fetch(logged: list[str], *, trunk: set[str], expansion: Expansion) -> bool:
    """Report whether any logged sha is off the local trunk, so origin may know more.

    An ambiguous sha is left out, since fetching more objects never makes it unique.
    """
    return any(
        expansion.full.get(sha) not in trunk for sha in logged if sha not in expansion.ambiguous
    )


def _tally(report: Report, decision: Decision) -> None:
    """Count one line's decision and note why an unchecked one was left."""
    match decision.action:
        case Action.REPLACE:
            report.replaced += 1
        case Action.DROP:
            report.dropped += 1
        case Action.IN_PROGRESS:
            report.in_progress += 1
        case Action.UNCHECKED:
            report.unchecked += 1
            if decision.reason not in report.notes:
                report.notes.append(decision.reason)
        case Action.KEEP:
            pass


RECONCILERS: tuple[Callable[..., Report], ...] = (log_commits,)


def run(context: Context, *, dry_run: bool) -> list[Report]:
    """Run every reconciler and return their reports in order.

    Args:
        context (Context): The project and its repository.
        dry_run (bool): Report what would change without writing.

    Returns:
        list[Report]: One report per reconciler.

    Raises:
        CommitsError: When git cannot answer. It is not caught here, so the caller
            decides how to fail.
    """
    return [reconciler(context, dry_run=dry_run) for reconciler in RECONCILERS]
