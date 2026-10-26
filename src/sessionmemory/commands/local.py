"""Commands that keep a checkout's private CLAUDE.local.md, skills, rules, and settings."""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import typer
from nclutils import ask, pp

from sessionmemory.commands._common import (
    VersionedGroup,
    emit_json,
    emit_value,
    fail,
    require_project,
    require_vault,
)
from sessionmemory.lib import localmd, paths

app = typer.Typer(
    cls=VersionedGroup,
    no_args_is_help=True,
    help=(
        "Keep this project's CLAUDE.local.md, private skills and rules, and "
        "settings.local.json in the vault and sync them with checkouts."
    ),
)

CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003
ADOPT = typer.Option(
    None,
    "--adopt",
    help="Adopt an entry: CLAUDE.local.md, skills/<name>, or rules/<name>. Repeatable.",
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


def _interactive() -> bool:
    """Report whether a person can answer a prompt, so an agent's call never blocks."""
    return sys.stdin.isatty() and sys.stdout.isatty()


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


@dataclass
class _Plan:
    """What one sync run was asked to do beyond the safe link work."""

    adopt: list[str] = field(default_factory=list)
    keep: Side | None = None


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


_LEAVE = "No, leave it alone"


def _confirm(question: str, yes: str) -> bool:
    """Ask one yes-or-no question whose answers say what each choice does.

    Returns:
        bool: True only when the person chose yes; cancelling counts as no.
    """
    return ask.choose_one_from_list(choices=[(yes, "yes"), (_LEAVE, "no")], message=question) == (
        "yes"
    )


def _ask_settings(pending: localmd.SettingsPending) -> Side | None:
    """Ask the one question that fits the settings state, naming what each answer writes.

    Returns:
        Side | None: The copy to keep, or None to leave both copies alone.
    """
    if pending.state is localmd.CopyState.CHECKOUT_ONLY:
        question = f"Track {pending.relpath} in the vault?"
        yes = "Yes, copy it into the vault (this checkout keeps its copy)"
        return "checkout" if _confirm(question=question, yes=yes) else None
    if pending.state is localmd.CopyState.VAULT_ONLY:
        question = (
            f"The vault has a {pending.relpath} for this project, but this checkout has none. "
            "Copy it into this checkout?"
        )
        return "vault" if _confirm(question=question, yes="Yes, copy it here") else None
    if pending.summary:
        pp.info(
            f"What each copy of {pending.relpath} has that the other lacks:",
            details=list(pending.summary),
        )
    choices: list[tuple[str, Side | Literal["neither"]]] = [
        (
            f"This checkout's (modified {pending.checkout_mtime}): copy it over the vault's",
            "checkout",
        ),
        (f"The vault's (modified {pending.vault_mtime}): copy it over this checkout's", "vault"),
        ("Neither: leave both as they are", "neither"),
    ]
    side = ask.choose_one_from_list(
        choices=choices,
        message=f"{pending.relpath} differs between this checkout and the vault. Which copy wins?",
    )
    return side if side in {"checkout", "vault"} else None


def _ask(waiting: list[localmd.Entry], pending: localmd.SettingsPending | None) -> _Plan:
    """Ask the person about each pending item in turn, one plain question per item."""
    adopt = [
        item.name
        for item in waiting
        if _confirm(
            question=f"Track {item.relpath} in the vault?",
            yes="Yes, move it into the vault and link it back here",
        )
    ]
    keep = _ask_settings(pending) if pending is not None else None
    return _Plan(adopt=adopt, keep=keep)


def _entries(co: localmd.Checkout, names: list[str]) -> tuple[list[localmd.Entry], bool]:
    """Build the link entries a person named, reporting each name that is not one.

    Returns:
        tuple[list[localmd.Entry], bool]: The valid entries, and whether any name failed.
    """
    items: list[localmd.Entry] = []
    failed = False
    for name in names:
        try:
            item = localmd.entry(co=co, name=name)
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
    co: localmd.Checkout, names: list[str], tracked: set[str]
) -> tuple[list[localmd.AdoptResult], bool]:
    """Adopt each named entry, warning once about rule approval.

    Returns:
        tuple[list[localmd.AdoptResult], bool]: The adopted entries, and whether any failed.
    """
    items, failed = _entries(co=co, names=names)
    if not items:
        return [], failed
    adopted: list[localmd.AdoptResult] = []
    for item in items:
        result = _adopt_one(co=co, item=item, tracked=item.name in tracked)
        if result is None:
            failed = True
        else:
            adopted.append(result)
    copied = [result for result in adopted if not result.already_linked]
    if any(result.entry.kind == "rule" for result in copied):
        pp.warning(
            "Claude Code asks once per checkout to allow a linked rule as an external import"
        )
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
            details=[f"Track them in the vault with: sessionmemory local sync {adopt} --cwd ."],
        )
    if pending is None:
        return
    keep = "sessionmemory local sync --keep-settings {side} --cwd ."
    if pending.state is localmd.CopyState.CHECKOUT_ONLY:
        pp.warning(
            f"{pending.relpath} is not tracked in the vault yet",
            details=[f"Track it in the vault with: {keep.format(side='checkout')}"],
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
    adopted: list[localmd.AdoptResult]
    kept: Path | None
    waiting: list[localmd.Entry]
    pending: localmd.SettingsPending | None

    def payload(self, plan: _Plan) -> dict[str, object]:
        """Build the `--json` payload; `adopted` and `settings` only for flags that ran."""
        payload: dict[str, object] = {
            "entries": _link_rows(self.results),
            "pending": {
                "adopt": [item.name for item in self.waiting],
                "settings": _settings_payload(self.pending),
            },
        }
        if plan.adopt:
            payload["adopted"] = [
                {"name": result.entry.name, "target": str(result.target)} for result in self.adopted
            ]
        if plan.keep is not None and self.kept is not None:
            payload["settings"] = {"kept": plan.keep, "path": str(self.kept)}
        return payload

    def prose(self) -> None:
        """Print written paths on stdout and everything else, pending work included, off it."""
        _report_links(self.results)
        for result in self.adopted:
            emit_value(str(result.target))
        if self.kept is not None:
            emit_value(str(self.kept))
        _report_pending(waiting=self.waiting, pending=self.pending)
        if not (self.results or self.adopted or self.kept or self.waiting or self.pending):
            pp.info("nothing to sync for this project")


@app.command(
    "sync",
    help=(
        "Link the vault's entries into this checkout, then adopt entries and keep a settings "
        "copy as asked. With no flags on a terminal, answer one question per pending item."
    ),
)
def sync_command(
    adopt: list[str] | None = ADOPT,
    keep_settings: Side | None = KEEP_SETTINGS,
    as_json: bool = JSON,  # noqa: FBT001
    cwd: Path | None = CWD,
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

    plan = _Plan(adopt=list(adopt or []), keep=keep_settings)
    flagged = bool(plan.adopt) or plan.keep is not None
    waiting, listed = _waiting(co, tracked) if not flagged else ([], True)
    pending, compared = _pending_settings(co, tracked) if not flagged else (None, True)
    if not flagged and not as_json and _interactive():
        plan = _ask(waiting=waiting, pending=pending)

    adopted, failed = (
        _adopt_all(co=co, names=plan.adopt, tracked=tracked) if plan.adopt else ([], False)
    )
    kept, changed = _keep(co=co, side=plan.keep, tracked=tracked) if plan.keep else (None, False)
    if any(not result.already_linked for result in adopted) or (
        changed and plan.keep == "checkout"
    ):
        pp.warning("the hooks commit the vault copy to the vault's git remote")
    failed = failed or (plan.keep is not None and kept is None)
    # Linking runs first, so an entry adopted by this run was in its way.
    done = {result.entry.name for result in adopted}
    results = [r for r in results if not (r.status == "conflict" and r.entry.name in done)]
    # Recomputed only after an adopt or keep, which change what is still pending.
    if plan.adopt or plan.keep is not None:
        waiting, listed = _waiting(co, tracked)
        pending, compared = _pending_settings(co, tracked)
    failed = failed or not (listed and compared)

    report = _Report(results=results, adopted=adopted, kept=kept, waiting=waiting, pending=pending)
    if as_json:
        emit_json(report.payload(plan))
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
def status_command(cwd: Path | None = CWD, as_json: bool = JSON) -> None:  # noqa: FBT001
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

    for row in rows:
        flags = _flags(row)
        suffix = f"  ({', '.join(flags)})" if flags else ""
        emit_value(f"{row['state']:<{_STATE_WIDTH}}  {row['name']}{suffix}")
    emit_value(f"local: {co.local}")
