"""Commands that keep a checkout's private CLAUDE.local.md, skills, rules, and settings."""

from __future__ import annotations

import contextlib
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer
from nclutils import pp

from sessionmemory.commands import _common
from sessionmemory.commands._common import (
    CWD_OPTION,
    JSON_OPTION,
    VersionedGroup,
    emit_json,
    emit_value,
    fail,
    require_registered,
    require_vault,
)
from sessionmemory.lib import localdeclined, localmanifest, localmd, paths

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

app = typer.Typer(
    cls=VersionedGroup,
    no_args_is_help=True,
    help="Keep private files such as CLAUDE.local.md in the vault and sync them to checkouts.",
)

ADOPT_NAMES = typer.Argument(
    ...,
    help=(
        "CLAUDE.local.md, skills/<name>, rules/<name>, or a path to any file or folder in "
        "this checkout."
    ),
    show_default=False,
)
DECLINE_NAMES = typer.Argument(
    ...,
    help="CLAUDE.local.md, skills/<name>, or rules/<name>, or the path to one in this checkout.",
    show_default=False,
)
KEEP_SETTINGS = typer.Option(
    None,
    "--keep-settings",
    help="Keep this copy of settings.local.json and write it over the other.",
    show_default=False,
)

# Wide enough for the longest link or copy state, so names line up.
_STATE_WIDTH = max(len(state) for state in [*localmd.State, *localmd.CopyState])
_DECLINE_ANSWER = "No, keep it here and stop asking"


def _checkout(cwd: Path | None) -> localmd.Checkout:
    """Resolve the project and locate the checkout, failing on any library error.

    Args:
        cwd (Path | None): The directory to resolve from, or the shell's directory.

    Returns:
        localmd.Checkout: The checkout holding `cwd`.
    """
    vault = require_vault()
    slug, resolved = require_registered(vault=vault, cwd=cwd)
    try:
        return localmd.checkout(
            vault=vault,
            slug=slug,
            cwd=cwd if cwd is not None else Path.cwd(),
            worktree=resolved.worktree_root,
        )
    except localmd.LocalMdError as err:
        fail(err.message, details=err.details)


@contextlib.contextmanager
def _or_fail(co: localmd.Checkout, doing: str) -> Generator[None]:
    """Exit on a library or filesystem error, naming what was being done to the checkout.

    Args:
        co (localmd.Checkout): The checkout being worked on.
        doing (str): What failed, as in "could not <doing> the entries of <worktree>".
    """
    try:
        yield
    except localmd.LocalMdError as err:
        fail(err.message, details=err.details)
    except OSError as err:
        fail(f"could not {doing} the entries of {co.worktree}: {err}")


def _migrate(co: localmd.Checkout) -> None:
    """Move an old-path vault copy into `local/` before anything looks at the entries."""
    try:
        legacy = localmd.migrate_legacy(vault=co.vault, slug=co.slug)
    except OSError as err:
        fail(f"could not move the vault's old {paths.LOCAL_MD_FILE} into {co.local}: {err}")
    # Both vault copies of CLAUDE.local.md differ, so neither was moved.
    if legacy is not None:
        pp.warning(
            localmd.legacy_conflict(legacy=legacy, target=co.local / paths.LOCAL_MD_FILE),
            details=[localmd.KEEP_ONE],
        )


Side = Literal["checkout", "vault"]


# How the person settled a conflict: point a foreign link at the vault, keep one side of
# a differing entry, or keep one of the two vault copies of CLAUDE.local.md.
Settle = Literal["relink", "checkout", "vault", "old", "local"]


@dataclass
class _Plan:
    """What one run was asked to settle, adopt, and keep beyond the safe link work."""

    adopt: list[str] = field(default_factory=list)
    decline: list[str] = field(default_factory=list)
    keep: Side | None = None
    settle: list[tuple[localmd.Entry, Settle]] = field(default_factory=list)
    left_alone: set[str] = field(default_factory=set)


def _pending_settings(
    co: localmd.Checkout, tracked: set[str]
) -> tuple[localmd.SettingsPending | None, bool]:
    """Describe the settings difference waiting for the person.

    A read error is reported rather than fatal, so the links this run made still reach
    the caller's payload.

    Returns:
        tuple[localmd.SettingsPending | None, bool]: The difference, and False on an error.
    """
    item = localmd.settings_entry(co)
    try:
        return localmd.settings_pending(co=co, tracked=item.name in tracked), True
    except localmd.LocalMdError as err:
        pp.error(err.message, details=err.details)
    except OSError as err:
        pp.error(f"could not read {item.link} or {item.target}: {err}")
    return None, False


def _waiting(co: localmd.Checkout, tracked: set[str]) -> tuple[list[localmd.Entry], bool]:
    """List the entries adopt could take, reporting a git error rather than failing.

    Returns:
        tuple[list[localmd.Entry], bool]: The entries, and False on an error.
    """
    try:
        return localmd.candidates(co, tracked=tracked), True
    except localmd.LocalMdError as err:
        pp.error(err.message, details=err.details)
    except OSError as err:
        pp.error(f"could not read the entries of {co.worktree}: {err}")
    return [], False


def _ask_settings(pending: localmd.SettingsPending) -> Side | None:
    """Ask the one question that fits the settings state, naming what each answer writes.

    Returns:
        Side | None: The copy to keep, or None to leave both copies alone.
    """
    if pending.state is localmd.CopyState.CHECKOUT_ONLY:
        question = f"Copy {pending.relpath} into the vault?"
        yes = "Yes, copy it into the vault (this checkout keeps its copy)"
        return "checkout" if _common.confirm(question=question, yes=yes) else None
    if pending.state is localmd.CopyState.VAULT_ONLY:
        question = (
            f"The vault has a {pending.relpath} for this project, but this checkout has none. "
            "Copy it into this checkout?"
        )
        return "vault" if _common.confirm(question=question, yes="Yes, copy it here") else None
    if pending.summary:
        pp.info(
            f"What each copy of {pending.relpath} has that the other lacks:",
            details=list(pending.summary),
        )
    return _which(
        f"{pending.relpath} differs between this checkout and the vault. Which copy wins?",
        [
            (f"local (modified {pending.checkout_mtime})", "checkout"),
            (f"vault (modified {pending.vault_mtime})", "vault"),
        ],
    )


_EXIT = "exit (leave both as they are)"


def _which[A: str](question: str, choices: list[tuple[str, A]]) -> A | None:
    """Ask which of two copies wins, offering exit as the last answer.

    Returns:
        A | None: The chosen action, or None for exit or a cancel.
    """
    # Imported here, like `_common.answer`, so only a run that asks pays for it.
    from nclutils import ask

    answer = ask.choose_one_from_list(choices=[*choices, (_EXIT, "exit")], message=question)
    return next((action for _, action in choices if action == answer), None)


def _settleable(co: localmd.Checkout, result: localmd.LinkResult) -> Settle | None:
    """Name the kind of conflict a person can settle by answering, or None for the rest.

    A conflict whose fix is outside the command, such as a symlinked `.claude/` or a
    misshapen vault entry, is reported as before.
    """
    item = result.entry
    try:
        if localmd.blocked(co, item) or localmd.target_misshapen(item):
            return None
        if item.kind == "local-md" and paths.legacy_local_md_path(co.vault, co.slug).is_file():
            return "old"
        if result.state is localmd.State.FOREIGN_LINK:
            return "relink"
        # An identical copy is offered with the adopt questions instead.
        if result.state is localmd.State.DIVERGED and localmd.contents_match(item) is False:
            return "checkout"
    except OSError:
        # An entry that cannot be read is reported as a conflict rather than asked about.
        return None
    return None


def _ask_conflict(co: localmd.Checkout, item: localmd.Entry, kind: Settle) -> Settle | None:
    """Ask the one question that settles a conflict, naming what each answer does.

    Returns:
        Settle | None: The action to take, or None to leave the conflict as it is.
    """
    if kind == "relink":
        question = (
            f"{item.relpath} is a link to {item.link.resolve()}, not to the vault copy. "
            "Point it at the vault copy?"
        )
        yes = "Yes, point it at the vault copy (what it points at now is kept)"
        return "relink" if _common.confirm(question=question, yes=yes) else None
    if kind == "old":
        old = paths.legacy_local_md_path(co.vault, co.slug)
        return _which(
            f"The vault holds two copies of {item.name} that differ. Which one wins?",
            [
                (f"old copy at {old} (modified {localmd.modified(old)})", "old"),
                (
                    f"local/ copy at {item.target} (modified {localmd.modified(item.target)})",
                    "local",
                ),
            ],
        )
    return _which(
        f"{item.relpath} differs from the vault copy. Which copy wins?",
        [
            (f"local (modified {localmd.modified(item.link)})", "checkout"),
            (f"vault (modified {localmd.modified(item.target)})", "vault"),
        ],
    )


def _ask_adopt(item: localmd.Entry) -> bool | None:
    """Ask whether to adopt one entry, or to link over a copy identical to the vault's.

    Returns:
        bool | None: True to adopt, False to decline for good, None when cancelled.
    """
    if localmd.state(item) is localmd.State.DIVERGED:
        return _common.answer(
            question=f"{item.relpath} is identical to the vault copy. Replace it with a link?",
            yes="Yes, replace it with a link to the vault copy",
            no=_DECLINE_ANSWER,
        )
    return _common.answer(
        question=f"Adopt {item.relpath} into the vault?",
        yes="Yes, move it into the vault and link it back here",
        no=_DECLINE_ANSWER,
    )


def _ask(
    co: localmd.Checkout,
    conflicts: list[localmd.LinkResult],
    waiting: list[localmd.Entry],
    pending: localmd.SettingsPending | None,
) -> _Plan:
    """Ask the person about each conflict and pending item in turn, one question per item."""
    plan = _Plan()
    for result in conflicts:
        kind = _settleable(co, result)
        if kind is None:
            continue
        action = _ask_conflict(co, result.entry, kind)
        if action is None:
            plan.left_alone.add(result.entry.name)
        else:
            plan.settle.append((result.entry, action))
    for item in waiting:
        choice = _ask_adopt(item)
        if choice is True:
            plan.adopt.append(item.name)
            continue
        if choice is False:
            plan.decline.append(item.name)
        plan.left_alone.add(item.name)
    plan.keep = _ask_settings(pending) if pending is not None else None
    return plan


def _entries(co: localmd.Checkout, names: list[str], cwd: Path) -> tuple[list[localmd.Entry], bool]:
    """Build the link entries a person named, reporting each name that is not one.

    Returns:
        tuple[list[localmd.Entry], bool]: The valid entries, and whether any name failed.
    """
    items: list[localmd.Entry] = []
    failed = False
    for name in names:
        try:
            item = localmd.resolve_adopt(co=co, raw=name, cwd=cwd)
        except localmd.LocalMdError as err:
            pp.error(err.message, details=err.details)
            failed = True
            continue
        items.append(item)
    return items, failed


def _adopt_one(
    co: localmd.Checkout, item: localmd.Entry, *, tracked: bool
) -> localmd.AdoptResult | None:
    """Adopt one entry, reporting a failure so the next entry still runs.

    Returns:
        localmd.AdoptResult | None: The outcome, or None when the entry failed.
    """
    try:
        result = localmd.adopt(co=co, item=item, tracked=tracked)
    except localmd.LocalMdError as err:
        pp.error(err.message, details=err.details)
        return None
    except OSError as err:
        pp.error(f"could not adopt {item.name}: {err}")
        return None
    if result.still_declined:
        pp.warning(
            f"{item.name} is still declined in {co.local / localdeclined.DECLINED_NAME}, "
            "which could not be updated; delete its line by hand"
        )
    for target in result.relative_imports:
        pp.warning(
            f"@{target} in {item.name} resolves from the vault copy's folder and will not load"
        )
    return result


def _adopt_all(
    co: localmd.Checkout, names: list[str], tracked: set[str], cwd: Path
) -> tuple[list[localmd.AdoptResult], bool]:
    """Adopt each named entry, reporting each failure so the rest still run.

    Returns:
        tuple[list[localmd.AdoptResult], bool]: The adopted entries, and whether any failed.
    """
    items, failed = _entries(co=co, names=names, cwd=cwd)
    if not items:
        return [], failed
    # A path named for the first time is in no listing git was asked about yet.
    try:
        tracked = tracked | localmd.tracked_names(
            co=co, items=[item for item in items if item.kind == "path"]
        )
    except localmd.LocalMdError as err:
        pp.error(err.message, details=err.details)
        return [], True
    adopted: list[localmd.AdoptResult] = []
    for item in items:
        result = _adopt_one(co=co, item=item, tracked=item.name in tracked)
        if result is None:
            failed = True
        else:
            adopted.append(result)
    return adopted, failed


def _decline(co: localmd.Checkout, names: list[str]) -> list[str] | None:
    """Record entries the person declined, reporting a list that cannot be updated.

    Returns:
        list[str] | None: The names newly recorded, or None when nothing could be written.
    """
    try:
        return localmd.decline(co, names)
    except localdeclined.DeclinedError as err:
        pp.error(err.message, details=err.details)
    except OSError as err:
        pp.error(f"could not update {co.local / localdeclined.DECLINED_NAME}: {err}")
    return None


def _keep(co: localmd.Checkout, side: Side, tracked: set[str]) -> tuple[Path | None, bool]:
    """Write the chosen settings copy over the other, reporting a refusal.

    Returns:
        tuple[Path | None, bool]: The path holding the kept copy, or None when the keep
            was refused, and whether anything was written.
    """
    item = localmd.settings_entry(co)
    try:
        kept = localmd.keep_settings(co=co, side=side, tracked=item.name in tracked)
    except localmd.LocalMdError as err:
        pp.error(err.message, details=err.details)
        return None, False
    except OSError as err:
        pp.error(f"could not copy {item.name}: {err}")
        return None, False
    if side == "vault" and kept.written:
        pp.warning(
            "Claude Code read its settings before this ran; start a new session before "
            "approving anything so it does not write the old copy back"
        )
    return kept.path, kept.written


_SETTLED: dict[Settle, Callable[[localmd.Entry], str]] = {
    "relink": lambda item: f"pointed {item.relpath} at the vault copy",
    "checkout": lambda item: f"kept this checkout's {item.relpath} and linked it to the vault",
    "vault": lambda item: f"kept the vault's {item.relpath} and linked it here",
    "old": lambda item: f"kept the vault's old copy of {item.name} and linked it here",
    "local": lambda item: f"kept the vault's local/ copy of {item.name} and linked it here",
}

_RULE_NOTICE = "Claude Code asks once per checkout to allow a linked rule as an external import"
_COMMIT_NOTICE = "the vault's hooks commit the vault copy to the vault's git history"


@dataclass
class _Done:
    """What carrying out one or more plans wrote, and what the person should know about it."""

    adopted: list[localmd.AdoptResult] = field(default_factory=list)
    kept: Path | None = None
    kept_side: Side | None = None
    failed: bool = False
    notices: list[str] = field(default_factory=list)

    settled: dict[str, tuple[localmd.LinkResult, Settle]] = field(default_factory=dict)

    def carry_out(self, co: localmd.Checkout, plan: _Plan, tracked: set[str]) -> None:
        """Settle, decline, adopt, and keep settings as `plan` asks, adding the outcome here."""
        if plan.decline and _decline(co, plan.decline) is None:
            self.failed = True
        for item, action in plan.settle:
            self._settle(co=co, item=item, action=action, tracked=item.name in tracked)
        if plan.adopt:
            adopted, failed = _adopt_all(co=co, names=plan.adopt, tracked=tracked, cwd=Path.cwd())
            self.adopted.extend(adopted)
            self.failed = self.failed or failed
        changed = False
        if plan.keep is not None:
            kept, changed = _keep(co=co, side=plan.keep, tracked=tracked)
            self.failed = self.failed or kept is None
            if kept is not None:
                self.kept, self.kept_side = kept, plan.keep
        copied = [result for result in self.adopted if not result.already_linked]
        if any(result.entry.kind == "rule" for result in copied):
            self._notice(_RULE_NOTICE)
        vault_written = any(
            action in {"checkout", "old", "local"} for _, action in self.settled.values()
        )
        if copied or vault_written or (changed and plan.keep == "checkout"):
            self._notice(_COMMIT_NOTICE)

    def _settle(
        self, co: localmd.Checkout, item: localmd.Entry, action: Settle, *, tracked: bool
    ) -> None:
        try:
            if action == "relink":
                result = localmd.relink(co=co, item=item, tracked=tracked)
            elif action in {"old", "local"}:
                result = localmd.keep_legacy(co=co, keep=action, tracked=tracked)
            else:
                result = localmd.keep_copy(co=co, item=item, side=action, tracked=tracked)
        except localmd.LocalMdError as err:
            pp.error(err.message, details=err.details)
            self.failed = True
            return
        except OSError as err:
            pp.error(f"could not settle {item.name}: {err}")
            self.failed = True
            return
        self.settled[item.name] = (result, action)

    def _notice(self, text: str) -> None:
        if text not in self.notices:
            self.notices.append(text)


def _adopted(result: localmd.AdoptResult) -> str:
    verb = "was already linked to" if result.already_linked else "moved into"
    return f"{result.entry.relpath} {verb} the vault"


def _report_links(results: list[localmd.LinkResult]) -> None:
    """Print each created link on stdout and explain every other outcome off it."""
    for result in results:
        if result.status == "created":
            emit_value(str(result.entry.link))
        elif result.status == "conflict":
            pp.warning(
                result.reason or f"{result.entry.name} was not linked",
                details=[result.remedy] if result.remedy else [],
            )
        elif result.status == "tracked":
            pp.warning(f"{result.entry.link} is tracked by git; the vault copy stays in the vault")
        elif result.status == "removed":
            pp.warning(f"removed {result.entry.link}, whose vault entry is gone")


def _report_pending(waiting: list[localmd.Entry], pending: localmd.SettingsPending | None) -> None:
    """Name each pending item and the command that resolves it, off stdout."""
    if waiting:
        names = " ".join(shlex.quote(item.name) for item in waiting)
        pp.warning(
            f"not tracked in the vault yet: {', '.join(item.relpath for item in waiting)}",
            details=[f"Adopt them into the vault with: sessionmemory local adopt {names} --cwd ."],
        )
    if pending is None:
        return
    keep = "sessionmemory local sync --keep-settings {side} --cwd ."
    if pending.state is localmd.CopyState.CHECKOUT_ONLY:
        pp.warning(
            f"{pending.relpath} is not tracked in the vault yet",
            details=[f"Copy it into the vault with: {keep.format(side='checkout')}"],
        )
    elif pending.state is localmd.CopyState.VAULT_ONLY:
        pp.warning(
            f"the vault has a {pending.relpath} that this checkout lacks",
            details=[f"Copy it into this checkout with: {keep.format(side='vault')}"],
        )
    else:
        pp.warning(
            f"{pending.relpath} differs between this checkout and the vault",
            details=[
                *pending.summary,
                f"Copy this checkout's over the vault's with: {keep.format(side='checkout')}",
                f"Copy the vault's over this checkout's with: {keep.format(side='vault')}",
            ],
        )


def _settings_payload(pending: localmd.SettingsPending | None) -> dict[str, object] | None:
    if pending is None:
        return None
    return {
        "state": str(pending.state),
        "checkout": {"path": str(pending.checkout), "mtime": pending.checkout_mtime},
        "vault": {"path": str(pending.vault), "mtime": pending.vault_mtime},
        "summary": list(pending.summary),
    }


def _link_rows(results: list[localmd.LinkResult]) -> list[dict[str, object]]:
    return [
        {
            "kind": result.entry.kind,
            "name": result.entry.name,
            "status": result.status,
            "link": str(result.entry.link),
            "target": str(result.entry.target),
            "reason": result.reason,
            "remedy": result.remedy,
        }
        for result in results
    ]


@dataclass(frozen=True)
class _Report:
    """Everything one sync run did and found, for its JSON or prose output."""

    results: list[localmd.LinkResult]
    done: _Done
    waiting: list[localmd.Entry]
    pending: localmd.SettingsPending | None
    declined: localdeclined.Declined
    declined_path: Path
    manifest_problems: tuple[str, ...] = ()

    def payload(self) -> dict[str, object]:
        """Build the `--json` payload; `settings` only when `--keep-settings` ran."""
        payload: dict[str, object] = {
            "entries": _link_rows(self.results),
            "pending": {
                "adopt": [item.name for item in self.waiting],
                "settings": _settings_payload(self.pending),
            },
            "manifest_problems": list(self.manifest_problems),
            "declined_problems": list(self.declined.problems),
        }
        if self.done.kept is not None:
            payload["settings"] = {"kept": self.done.kept_side, "path": str(self.done.kept)}
        return payload

    @property
    def _empty(self) -> bool:
        return not (
            self.results or self.done.adopted or self.done.kept or self.waiting or self.pending
        )

    def prose(self) -> None:
        """Print written paths on stdout and everything else, pending work included, off it."""
        _report_links(self.results)
        for result in self.done.adopted:
            emit_value(str(result.target))
        if self.done.kept is not None:
            emit_value(str(self.done.kept))
        for notice in self.done.notices:
            pp.warning(notice)
        _report_pending(waiting=self.waiting, pending=self.pending)
        if self._empty:
            pp.info("nothing to sync for this project")

    def terminal(self) -> None:
        """Tell a person at a terminal what changed, leaving out what they just declined."""
        for result in self.results:
            if result.status != "created":
                _report_links([result])
            elif result.entry.name in self.done.settled:
                pp.success(_SETTLED[self.done.settled[result.entry.name][1]](result.entry))
            else:
                pp.success(f"linked {result.entry.relpath} from the vault")
        for result in self.done.adopted:
            pp.success(_adopted(result))
        if self.done.kept is not None:
            source, dest = (
                ("this checkout's", "the vault's")
                if self.done.kept_side == "checkout"
                else ("the vault's", "this checkout's")
            )
            pp.success(f"copied {source} settings.local.json over {dest}")
        for notice in self.done.notices:
            pp.info(notice)
        if self.declined.names:
            count = len(self.declined.names)
            noun = "entry is" if count == 1 else "entries are"
            pp.info(
                f"{count} declined {noun} not offered; remove a name from "
                f"{self.declined_path} to be asked again"
            )
        if self._empty:
            pp.info("nothing to sync for this project")


@app.command(
    "sync",
    help=(
        "Link the vault's entries into this checkout. On a terminal, answer one question per "
        "item still pending."
    ),
)
def sync_command(
    keep_settings: Side | None = KEEP_SETTINGS,
    as_json: bool = JSON_OPTION,  # noqa: FBT001
    cwd: Path | None = CWD_OPTION,
) -> None:
    """Link the vault's entries into this checkout, then settle what the person chooses.

    Raises:
        Exit: When an entry is in conflict, or an adopt or keep the person chose failed.
    """
    co = _checkout(cwd)
    with _or_fail(co, "link"):
        # One listing of git's index serves linking, the pending report, and every answer.
        tracked = localmd.tracked_all(co)
        results = localmd.link_all(co, tracked=tracked)

    problems = localmd.manifest(co).problems
    if problems:
        pp.error(
            f"{co.local / localmanifest.MANIFEST_NAME} lists paths that cannot be linked",
            details=[*problems, "Fix the file, then run local sync."],
        )
    unusable = localmd.declined(co).problems
    if unusable:
        pp.error(
            f"{co.local / localdeclined.DECLINED_NAME} cannot be fully read, so sync may offer "
            "entries it lists",
            details=[*unusable, "Fix the file, then run local sync."],
        )

    # A person is asked about whatever the flags left pending, rather than handed commands.
    ask_inline = not as_json and _common.interactive()
    plan = _Plan(keep=keep_settings)
    done = _Done()
    done.carry_out(co=co, plan=plan, tracked=tracked)
    waiting, listed = _waiting(co, tracked)
    pending, compared = _pending_settings(co, tracked)
    left_alone: set[str] = set()
    if ask_inline:
        conflicts = [r for r in results if r.status == "conflict"]
        answers = _ask(co=co, conflicts=conflicts, waiting=waiting, pending=pending)
        left_alone = answers.left_alone
        if answers.settle or answers.adopt or answers.decline or answers.keep is not None:
            done.carry_out(co=co, plan=answers, tracked=tracked)
            waiting, listed = _waiting(co, tracked)
            pending, compared = _pending_settings(co, tracked)
    # Linking runs first, so an entry adopted by this run was in its way. A conflict the
    # person settled shows its new outcome, and one they chose to leave is not repeated.
    # A declined copy identical to the vault's is the person's to keep, so it is quiet.
    declined = localmd.declined(co)
    adopted_names = {result.entry.name for result in done.adopted}
    quiet = (
        adopted_names
        | left_alone
        | {
            r.entry.name
            for r in results
            if r.status == "conflict"
            and r.entry.name in declined.names
            and localmd.identical(r.entry)
        }
    )
    results = [
        done.settled[r.entry.name][0] if r.entry.name in done.settled else r
        for r in results
        if not (r.status == "conflict" and r.entry.name in quiet)
    ]
    failed = done.failed or not (listed and compared) or bool(problems)

    report = _Report(
        results=results,
        done=done,
        waiting=waiting,
        pending=pending,
        manifest_problems=problems,
        declined=declined,
        declined_path=co.local / localdeclined.DECLINED_NAME,
    )
    if as_json:
        emit_json(report.payload())
    elif ask_inline:
        report.terminal()
    else:
        report.prose()
    if failed or any(result.status == "conflict" for result in results):
        raise typer.Exit(1)


@app.command(
    "adopt",
    help="Move entries from this checkout into the vault and link each one back.",
)
def adopt_command(
    names: list[str] = ADOPT_NAMES,
    as_json: bool = JSON_OPTION,  # noqa: FBT001
    cwd: Path | None = CWD_OPTION,
) -> None:
    """Move entries from this checkout into the vault and link each one back.

    Raises:
        Exit: When any named entry could not be adopted.
    """
    co = _checkout(cwd)
    _migrate(co)
    with _or_fail(co, "read"):
        tracked = localmd.tracked_all(co)
    done = _Done()
    # A relative path reads from the shell's directory, not --cwd, as a shell would.
    done.carry_out(co=co, plan=_Plan(adopt=names), tracked=tracked)

    if as_json:
        emit_json(
            {
                "adopted": [
                    {"name": result.entry.name, "target": str(result.target)}
                    for result in done.adopted
                ]
            }
        )
    elif _common.interactive():
        for result in done.adopted:
            pp.success(_adopted(result))
        for notice in done.notices:
            pp.info(notice)
    else:
        for result in done.adopted:
            emit_value(str(result.target))
        for notice in done.notices:
            pp.warning(notice)
    if done.failed:
        raise typer.Exit(1)


def _declinable(co: localmd.Checkout, raw: str, cwd: Path, present: list[str]) -> str | None:
    """Return the entry name `raw` means, or None after reporting why it cannot be declined."""
    try:
        return localmd.declinable(co=co, raw=raw, cwd=cwd, present=present)
    except localmd.LocalMdError as err:
        pp.error(err.message, details=err.details)
    except OSError as err:
        pp.error(f"could not read {raw} in {co.worktree}: {err}")
    return None


@app.command(
    "decline",
    help="Keep entries out of the vault, so local sync and session start never offer them again.",
)
def decline_command(
    names: list[str] = DECLINE_NAMES,
    as_json: bool = JSON_OPTION,  # noqa: FBT001
    cwd: Path | None = CWD_OPTION,
) -> None:
    """Record entries the person keeps out of the vault, so no checkout offers them again.

    Raises:
        Exit: When any name was refused or the declined list could not be written.
    """
    co = _checkout(cwd)
    _migrate(co)
    with _or_fail(co, "read"):
        present = [item.name for item in localmd.entries(co)]
    # A relative path reads from the shell's directory, not --cwd, as a shell would.
    resolved = [_declinable(co=co, raw=raw, cwd=Path.cwd(), present=present) for raw in names]
    wanted = [name for name in resolved if name is not None]
    added = _decline(co, wanted) if wanted else []
    if as_json:
        emit_json({"declined": added or []})
    else:
        for name in added or []:
            emit_value(name)
    if added is None or len(wanted) < len(names):
        raise typer.Exit(1)


def _flags(row: dict[str, object]) -> list[str]:
    """List what is surprising about an entry: tracked, an unignored link, or the contents."""
    flags: list[str] = []
    if row["tracked"]:
        flags.append("tracked")
    elif (
        row["kind"] != "settings"
        and row["state"] == localmd.State.LINKED
        and row["ignored"] is False
    ):
        flags.append("not ignored")
    if row["state"] == localmd.State.DIVERGED and row["match"] is not None:
        flags.append(f"contents {'identical' if row['match'] else 'different'}")
    return flags


def _status_row(
    item: localmd.Entry,
    *,
    tracked: bool,
    ignored: bool | None,
    state: str,
    match: bool | None,
) -> dict[str, object]:
    """Describe one entry for `status`, in the shape `--json` prints.

    Returns:
        dict[str, object]: The entry's kind, name, state, paths, and git and content flags.
    """
    return {
        "kind": item.kind,
        "name": item.name,
        "state": state,
        "link": str(item.link),
        "target": str(item.target),
        "ignored": ignored,
        "tracked": tracked,
        "match": match,
    }


@app.command("status", help="Report how each of this checkout's entries relates to the vault.")
def status_command(cwd: Path | None = CWD_OPTION, as_json: bool = JSON_OPTION) -> None:  # noqa: FBT001
    """Report how each of this checkout's entries relates to the vault.

    Raises:
        Exit: When git or the filesystem cannot be read.
    """
    co = _checkout(cwd)
    _migrate(co)
    with _or_fail(co, "read"):
        items = localmd.entries(co)
        settings = localmd.settings_entry(co)
        everything = [*items, settings]
        tracked = localmd.tracked_names(co=co, items=everything)
        # Git cannot be asked about a path behind a symlinked or non-folder `.claude/`.
        askable = [item for item in everything if not localmd.blocked(co=co, item=item)]
        ignored = localmd.ignored_names(co=co, items=askable)
        asked = {item.name for item in askable}

        def is_ignored(item: localmd.Entry) -> bool | None:
            return item.name in ignored if item.name in asked else None

        rows = [
            _status_row(
                item=item,
                tracked=item.name in tracked,
                ignored=is_ignored(item),
                state=str(localmd.state(item)),
                match=localmd.contents_match(item),
            )
            for item in items
        ]
        # The settings file is copied, never linked, so its copy state stands in.
        copy_state = localmd.copy_state(co=co, item=settings, tracked=settings.name in tracked)
        if copy_state is not None:
            rows.append(
                _status_row(
                    item=settings,
                    tracked=settings.name in tracked,
                    ignored=is_ignored(settings),
                    state=str(copy_state),
                    match=None,
                )
            )

    if as_json:
        emit_json({"local": str(co.local), "entries": rows})
        return

    if not rows:
        emit_value("no local entries in this checkout or the vault")
    for row in rows:
        flags = _flags(row)
        suffix = f"  ({', '.join(flags)})" if flags else ""
        emit_value(f"{row['state']:<{_STATE_WIDTH}}  {row['name']}{suffix}")
    emit_value(f"local: {co.local}")
