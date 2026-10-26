"""Commands that keep a checkout's private CLAUDE.local.md, skills, rules, and settings."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer
from nclutils import ask, pp

from sessionmemory.commands import _common
from sessionmemory.commands._common import (
    CWD_OPTION,
    VersionedGroup,
    emit_json,
    emit_value,
    fail,
    require_project,
    require_vault,
)
from sessionmemory.lib import localmanifest, localmd, paths

if TYPE_CHECKING:
    from collections.abc import Callable

app = typer.Typer(
    cls=VersionedGroup,
    no_args_is_help=True,
    help="Keep private files such as CLAUDE.local.md in the vault and sync them to checkouts.",
)

JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003
ADOPT = typer.Option(
    None,
    "--adopt",
    help=(
        "Adopt an entry: CLAUDE.local.md, skills/<name>, rules/<name>, or a path to any "
        "file or folder in this checkout. Repeatable."
    ),
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


def _checkout(cwd: Path | None) -> localmd.Checkout:
    """Resolve the project and locate the checkout, failing on any library error.

    Args:
        cwd (Path | None): The directory to resolve from, or the shell's directory.

    Returns:
        localmd.Checkout: The checkout holding `cwd`.
    """
    vault = require_vault()
    slug = require_project(vault=vault, cwd=cwd)
    try:
        return localmd.checkout(vault=vault, slug=slug, cwd=cwd if cwd is not None else Path.cwd())
    except localmd.LocalMdError as err:
        fail(err.message, details=err.details)


def _warn_legacy(co: localmd.Checkout, legacy: Path) -> None:
    """Name both vault copies of CLAUDE.local.md when they differ and neither was moved."""
    pp.warning(
        localmd.legacy_conflict(legacy=legacy, target=co.local / paths.LOCAL_MD_FILE),
        details=[localmd.KEEP_ONE],
    )


def _migrate(co: localmd.Checkout) -> None:
    """Move an old-path vault copy into `local/` before anything looks at the entries."""
    try:
        legacy = localmd.migrate_legacy(vault=co.vault, slug=co.slug)
    except OSError as err:
        fail(f"could not move the vault's old {paths.LOCAL_MD_FILE} into {co.local}: {err}")
    if legacy is not None:
        _warn_legacy(co=co, legacy=legacy)


Side = Literal["checkout", "vault"]


# How the person settled a conflict: point a foreign link at the vault, keep one side of
# a differing entry, or keep one of the two vault copies of CLAUDE.local.md.
Settle = Literal["relink", "checkout", "vault", "old", "local"]


@dataclass
class _Plan:
    """What one sync run was asked to do beyond the safe link work."""

    adopt: list[str] = field(default_factory=list)
    keep: Side | None = None
    settle: list[tuple[localmd.Entry, Settle]] = field(default_factory=list)
    declined: set[str] = field(default_factory=set)


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
    choices: list[tuple[str, Side | Literal["neither"]]] = [
        (f"local (modified {pending.checkout_mtime})", "checkout"),
        (f"vault (modified {pending.vault_mtime})", "vault"),
        ("exit (leave both as they are)", "neither"),
    ]
    side = ask.choose_one_from_list(
        choices=choices,
        message=f"{pending.relpath} differs between this checkout and the vault. Which copy wins?",
    )
    return side if side in {"checkout", "vault"} else None


_EXIT = "exit (leave both as they are)"


def _which(question: str, choices: list[tuple[str, Settle]]) -> Settle | None:
    """Ask which of two copies wins, offering exit as the last answer.

    Returns:
        Settle | None: The chosen action, or None for exit or a cancel.
    """
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


def _ask_adopt(item: localmd.Entry) -> bool:
    """Ask whether to adopt one entry, or to link over a copy identical to the vault's."""
    if localmd.state(item) is localmd.State.DIVERGED:
        return _common.confirm(
            question=f"{item.relpath} is identical to the vault copy. Replace it with a link?",
            yes="Yes, replace it with a link to the vault copy",
        )
    return _common.confirm(
        question=f"Adopt {item.relpath} into the vault?",
        yes="Yes, move it into the vault and link it back here",
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
            plan.declined.add(result.entry.name)
        else:
            plan.settle.append((result.entry, action))
    for item in waiting:
        if _ask_adopt(item):
            plan.adopt.append(item.name)
        else:
            plan.declined.add(item.name)
    plan.keep = _ask_settings(pending) if pending is not None else None
    return plan


def _entries(
    co: localmd.Checkout, names: list[str], cwd: Path, *, settings_asked: bool = False
) -> tuple[list[localmd.Entry], bool]:
    """Build the link entries a person named, reporting each name that is not one.

    With `settings_asked`, naming the settings file is skipped quietly, since the settings
    question that follows is the answer to it.

    Returns:
        tuple[list[localmd.Entry], bool]: The valid entries, and whether any name failed.
    """
    items: list[localmd.Entry] = []
    failed = False
    for name in names:
        try:
            item = localmd.resolve_adopt(co=co, raw=name, cwd=cwd)
        except localmd.SettingsNotAdoptedError as err:
            if not settings_asked:
                pp.error(err.message, details=err.details)
                failed = True
            continue
        except localmd.LocalMdError as err:
            pp.error(err.message, details=err.details)
            failed = True
            continue
        if item.mode == "copy":
            pp.error(
                f"{name} is kept by copy, not adopted",
                details=["Choose a copy with --keep-settings checkout or --keep-settings vault."],
            )
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
    for target in result.relative_imports:
        pp.warning(
            f"@{target} in {item.name} resolves from the vault copy's folder and will not load"
        )
    return result


def _adopt_all(
    co: localmd.Checkout,
    names: list[str],
    tracked: set[str],
    cwd: Path,
    *,
    settings_asked: bool = False,
) -> tuple[list[localmd.AdoptResult], bool]:
    """Adopt each named entry, reporting each failure so the rest still run.

    Returns:
        tuple[list[localmd.AdoptResult], bool]: The adopted entries, and whether any failed.
    """
    items, failed = _entries(co=co, names=names, cwd=cwd, settings_asked=settings_asked)
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


def _keep(co: localmd.Checkout, side: Side, tracked: set[str]) -> tuple[Path | None, bool]:
    """Write the chosen settings copy over the other, reporting a refusal.

    Returns:
        tuple[Path | None, bool]: The path holding the kept copy, or None when the keep
            was refused, and whether anything was written.
    """
    item = localmd.settings_entry(co)
    try:
        current = localmd.copy_state(co=co, item=item, tracked=item.name in tracked)
        written = localmd.keep_settings(co=co, side=side, tracked=item.name in tracked)
    except localmd.LocalMdError as err:
        pp.error(err.message, details=err.details)
        return None, False
    except OSError as err:
        pp.error(f"could not copy {item.name}: {err}")
        return None, False
    changed = current is not localmd.CopyState.SAME
    if side == "vault" and changed:
        pp.warning(
            "Claude Code read its settings before this ran; start a new session before "
            "approving anything so it does not write the old copy back"
        )
    return written, changed


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
    settings_asked: bool = False

    settled: dict[str, tuple[localmd.LinkResult, Settle]] = field(default_factory=dict)

    def carry_out(self, co: localmd.Checkout, plan: _Plan, tracked: set[str]) -> None:
        """Settle, adopt, and keep settings as `plan` asks, adding the outcome to this record."""
        for item, action in plan.settle:
            self._settle(co=co, item=item, action=action, tracked=item.name in tracked)
        if plan.adopt:
            adopted, failed = _adopt_all(
                co=co,
                names=plan.adopt,
                tracked=tracked,
                cwd=Path.cwd(),
                settings_asked=self.settings_asked,
            )
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


def _link_warning(result: localmd.LinkResult) -> None:
    """Explain a conflict and the remedy that fits its cause.

    Args:
        result (localmd.LinkResult): A `conflict` result from `link_all`.
    """
    pp.warning(
        result.reason or f"{result.entry.name} was not linked",
        details=[result.remedy] if result.remedy else [],
    )


def _report_links(results: list[localmd.LinkResult]) -> None:
    """Print each created link on stdout and explain every other outcome off it."""
    for result in results:
        if result.status == "created":
            emit_value(str(result.entry.link))
        elif result.status == "conflict":
            _link_warning(result)
        elif result.status == "tracked":
            pp.warning(f"{result.entry.link} is tracked by git; the vault copy stays in the vault")
        elif result.status == "removed":
            pp.warning(f"removed {result.entry.link}, whose vault entry is gone")


def _report_pending(waiting: list[localmd.Entry], pending: localmd.SettingsPending | None) -> None:
    """Name each pending item and the command that resolves it, off stdout."""
    if waiting:
        adopt = " ".join(f"--adopt {shlex.quote(item.name)}" for item in waiting)
        pp.warning(
            f"not tracked in the vault yet: {', '.join(item.relpath for item in waiting)}",
            details=[f"Adopt them into the vault with: sessionmemory local sync {adopt} --cwd ."],
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
    manifest_problems: tuple[str, ...] = ()

    def payload(self, plan: _Plan) -> dict[str, object]:
        """Build the `--json` payload; `adopted` and `settings` only for flags that ran."""
        payload: dict[str, object] = {
            "entries": _link_rows(self.results),
            "pending": {
                "adopt": [item.name for item in self.waiting],
                "settings": _settings_payload(self.pending),
            },
            "manifest_problems": list(self.manifest_problems),
        }
        if plan.adopt:
            payload["adopted"] = [
                {"name": result.entry.name, "target": str(result.target)}
                for result in self.done.adopted
            ]
        if plan.keep is not None and self.done.kept is not None:
            payload["settings"] = {"kept": plan.keep, "path": str(self.done.kept)}
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
            verb = "was already linked to" if result.already_linked else "moved into"
            pp.success(f"{result.entry.relpath} {verb} the vault")
        if self.done.kept is not None:
            source, dest = (
                ("this checkout's", "the vault's")
                if self.done.kept_side == "checkout"
                else ("the vault's", "this checkout's")
            )
            pp.success(f"copied {source} settings.local.json over {dest}")
        for notice in self.done.notices:
            pp.info(notice)
        if self._empty:
            pp.info("nothing to sync for this project")


@app.command(
    "sync",
    help=(
        "Link the vault's entries into this checkout, then adopt entries and keep a settings "
        "copy as asked. On a terminal, answer one question per item still pending."
    ),
)
def sync_command(
    adopt: list[str] | None = ADOPT,
    keep_settings: Side | None = KEEP_SETTINGS,
    as_json: bool = JSON,  # noqa: FBT001
    cwd: Path | None = CWD_OPTION,
) -> None:
    """Link the vault's entries into this checkout, then adopt and keep settings as asked.

    Raises:
        Exit: When an entry is in conflict, or a requested adopt or keep failed.
    """
    co = _checkout(cwd)
    try:
        # One listing of git's index serves linking, the pending report, adopt, and keep.
        tracked = localmd.tracked_all(co)
        results = localmd.link_all(co, tracked=tracked)
    except localmd.LocalMdError as err:
        fail(err.message, details=err.details)
    except OSError as err:
        fail(f"could not link the entries of {co.worktree}: {err}")

    problems = localmd.manifest(co).problems
    if problems:
        pp.error(
            f"{co.local / localmanifest.MANIFEST_NAME} lists paths that cannot be linked",
            details=[*problems, "Fix the file, then run local sync."],
        )

    # A person is asked about whatever the flags left pending, rather than handed commands.
    ask_inline = not as_json and _common.interactive()
    plan = _Plan(adopt=list(adopt or []), keep=keep_settings)
    done = _Done(settings_asked=ask_inline)
    done.carry_out(co=co, plan=plan, tracked=tracked)
    waiting, listed = _waiting(co, tracked)
    pending, compared = _pending_settings(co, tracked)
    declined: set[str] = set()
    if ask_inline:
        adopted_now = {result.entry.name for result in done.adopted}
        conflicts = [
            r for r in results if r.status == "conflict" and r.entry.name not in adopted_now
        ]
        answers = _ask(co=co, conflicts=conflicts, waiting=waiting, pending=pending)
        declined = answers.declined
        if answers.settle or answers.adopt or answers.keep is not None:
            done.carry_out(co=co, plan=answers, tracked=tracked)
            waiting, listed = _waiting(co, tracked)
            pending, compared = _pending_settings(co, tracked)
    # Linking runs first, so an entry adopted by this run was in its way. A conflict the
    # person settled shows its new outcome, and one they chose to leave is not repeated.
    adopted = {result.entry.name for result in done.adopted}
    results = [
        done.settled[r.entry.name][0] if r.entry.name in done.settled else r
        for r in results
        if not (r.status == "conflict" and (r.entry.name in adopted or r.entry.name in declined))
    ]
    failed = done.failed or not (listed and compared) or bool(problems)

    report = _Report(
        results=results,
        done=done,
        waiting=waiting,
        pending=pending,
        manifest_problems=problems,
    )
    if as_json:
        emit_json(report.payload(plan))
    elif ask_inline:
        report.terminal()
    else:
        report.prose()
    if failed or any(result.status == "conflict" for result in results):
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


def _status_row(co: localmd.Checkout, item: localmd.Entry, *, tracked: bool) -> dict[str, object]:
    """Describe one entry for `status`, in the shape `--json` prints.

    Returns:
        dict[str, object]: The entry's kind, name, state, paths, and git and content flags.
    """
    return {
        "kind": item.kind,
        "name": item.name,
        "state": str(localmd.state(item)),
        "link": str(item.link),
        "target": str(item.target),
        # Git cannot be asked about a path behind a symlinked or non-folder `.claude/`.
        "ignored": None if localmd.blocked(co=co, item=item) else localmd.ignored(co=co, item=item),
        "tracked": tracked,
        "match": localmd.contents_match(item),
    }


def _settings_row(
    co: localmd.Checkout, item: localmd.Entry, *, tracked: bool
) -> dict[str, object] | None:
    """Describe the settings entry for `status`, or None when neither side has a copy.

    Returns:
        dict[str, object] | None: The row, with the copy state in place of a link state.
    """
    current = localmd.copy_state(co=co, item=item, tracked=tracked)
    if current is None:
        return None
    return {
        "kind": item.kind,
        "name": item.name,
        "state": str(current),
        "link": str(item.link),
        "target": str(item.target),
        "ignored": None if localmd.blocked(co=co, item=item) else localmd.ignored(co=co, item=item),
        "tracked": tracked,
        "match": None,
    }


@app.command("status", help="Report how each of this checkout's entries relates to the vault.")
def status_command(cwd: Path | None = CWD_OPTION, as_json: bool = JSON) -> None:  # noqa: FBT001
    """Report how each of this checkout's entries relates to the vault.

    Raises:
        Exit: When git or the filesystem cannot be read.
    """
    co = _checkout(cwd)
    _migrate(co)
    try:
        items = localmd.entries(co)
        settings = localmd.settings_entry(co)
        tracked = localmd.tracked_names(co=co, items=[*items, settings])
        rows = [_status_row(co=co, item=item, tracked=item.name in tracked) for item in items]
        settings_row = _settings_row(co=co, item=settings, tracked=settings.name in tracked)
        if settings_row is not None:
            rows.append(settings_row)
    except localmd.LocalMdError as err:
        fail(err.message, details=err.details)
    except OSError as err:
        fail(f"could not read the entries of {co.worktree}: {err}")

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
