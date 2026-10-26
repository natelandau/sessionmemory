"""Commands that keep a checkout's private CLAUDE.local.md, skills, and rules in the vault."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

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
        "Keep this project's CLAUDE.local.md and private skills and rules in the vault "
        "and link them into checkouts."
    ),
)

CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003
NAMES = typer.Argument(
    None,
    help="Entries to adopt: CLAUDE.local.md, skills/<name>, or rules/<name>.",
    show_default=False,
)

# Wide enough for the longest state, `foreign link`, so names line up.
_STATE_WIDTH = max(len(state) for state in localmd.State)


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


def _selected(co: localmd.Checkout, names: list[str] | None) -> list[str]:
    """Name the entries to adopt, from the arguments or the checklist.

    Returns:
        list[str]: The names to adopt, empty when there is nothing to do.

    Raises:
        Exit: Off a terminal with no names, when candidates are waiting.
    """
    if names:
        return names
    try:
        waiting = localmd.candidates(co)
    except localmd.LocalMdError as err:
        fail(err.message, details=err.details)
    if not waiting:
        pp.info("nothing in this checkout is waiting to be adopted")
        return []
    listed = " ".join(shlex.quote(item.name) for item in waiting)
    if not _interactive():
        fail(
            f"not in the vault yet: {', '.join(item.name for item in waiting)}",
            details=[f"Adopt them with: sessionmemory local adopt {listed} --cwd ."],
        )
    chosen = ask.choose_multiple_from_list(
        choices=[(item.name, item) for item in waiting],
        message="Choose the entries to keep in the vault",
    )
    return [item.name for item in chosen or []]


def _entries(co: localmd.Checkout, names: list[str]) -> tuple[list[localmd.Entry], bool]:
    """Build the entries a person named, reporting each name that is not one.

    Returns:
        tuple[list[localmd.Entry], bool]: The valid entries, and whether any name failed.
    """
    items: list[localmd.Entry] = []
    failed = False
    for name in names:
        try:
            items.append(localmd.entry(co=co, name=name))
        except localmd.LocalMdError as err:
            pp.error(err.message, details=err.details)
            failed = True
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


@app.command(
    "adopt",
    help=(
        "Move entries from this checkout into the vault and link them back. With no names, "
        "choose from a checklist."
    ),
)
def adopt_command(
    names: list[str] | None = NAMES,
    cwd: Path | None = CWD,
) -> None:
    """Move entries from this checkout into the vault and link them back.

    Raises:
        Exit: When any entry cannot be adopted, or off a terminal with no names given.
    """
    co = _checkout(cwd)
    _migrate(co)
    chosen = _selected(co=co, names=names)
    if not chosen:
        return

    items, failed = _entries(co=co, names=chosen)
    try:
        tracked = localmd.tracked_names(co=co, items=items)
    except localmd.LocalMdError as err:
        fail(err.message, details=err.details)

    copied = False
    rule_linked = False
    for item in items:
        result = _adopt_one(co=co, item=item, tracked=item.name in tracked)
        if result is None:
            failed = True
            continue
        emit_value(str(result.target))
        if not result.already_linked:
            copied = True
            rule_linked = rule_linked or item.kind == "rule"

    if copied:
        pp.warning("the hooks commit the vault copy to the vault's git remote")
    if rule_linked:
        pp.warning(
            "Claude Code asks once per checkout to allow a linked rule as an external import"
        )
    if failed:
        raise typer.Exit(1)


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
    if not results:
        pp.info("the vault holds nothing local for this project")
    for result in results:
        if result.status == "created":
            emit_value(str(result.entry.link))
        elif result.status == "conflict":
            _link_warning(result)
        elif result.status == "tracked":
            pp.warning(f"{result.entry.link} is tracked by git; the vault copy stays in the vault")
        elif result.status == "removed":
            pp.warning(f"removed {result.entry.link}, whose vault entry is gone")


@app.command("link", help="Link every entry the vault holds into this checkout when that is safe.")
def link_command(cwd: Path | None = CWD, as_json: bool = JSON) -> None:  # noqa: FBT001
    """Link every entry the vault holds into this checkout when that is safe.

    Raises:
        Exit: When any entry is in conflict.
    """
    co = _checkout(cwd)
    try:
        results = localmd.link_all(co)
    except localmd.LocalMdError as err:
        fail(err.message, details=err.details)
    except OSError as err:
        fail(f"could not link the entries of {co.worktree}: {err}")
    conflict = any(result.status == "conflict" for result in results)

    if as_json:
        emit_json(
            {
                "entries": [
                    {
                        "kind": result.entry.kind,
                        "name": result.entry.name,
                        "status": result.status,
                        "link": str(result.entry.link),
                        "target": str(result.entry.target),
                    }
                    for result in results
                ]
            }
        )
    else:
        _report_links(results)
    if conflict:
        raise typer.Exit(1)


def _flags(row: dict[str, object]) -> list[str]:
    """List what is surprising about an entry: tracked, an unignored link, or the contents."""
    flags: list[str] = []
    if row["tracked"]:
        flags.append("tracked")
    elif row["state"] == localmd.State.LINKED and row["ignored"] is False:
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
        tracked = localmd.tracked_names(co=co, items=items)
        rows = [_status_row(co=co, item=item, tracked=item.name in tracked) for item in items]
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
