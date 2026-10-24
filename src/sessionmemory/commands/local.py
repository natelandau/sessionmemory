"""Commands that keep a checkout's CLAUDE.local.md in the vault and link it back."""

from __future__ import annotations

from pathlib import Path

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    VersionedGroup,
    emit_json,
    emit_value,
    fail,
    require_project,
    require_vault,
)
from sessionmemory.lib import localmd

app = typer.Typer(
    cls=VersionedGroup,
    no_args_is_help=True,
    help="Keep this project's CLAUDE.local.md in the vault and link it into checkouts.",
)

CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def _checkout(cwd: Path | None) -> localmd.Checkout:
    """Resolve the project and locate the checkout, failing on any library error.

    Args:
        cwd (Path | None): The directory to resolve from, or the shell's directory.

    Returns:
        localmd.Checkout: The checkout holding `cwd`.
    """
    vault = require_vault()
    slug = require_project(vault, cwd)
    try:
        return localmd.checkout(vault, slug, cwd if cwd is not None else Path.cwd())
    except localmd.LocalMdError as err:
        fail(err.message, err.details)


@app.command(
    "adopt",
    help="Move this checkout's CLAUDE.local.md into the vault and link it back.",
)
def adopt_command(cwd: Path | None = CWD) -> None:
    """Move this checkout's CLAUDE.local.md into the vault and link it back.

    Raises:
        Exit: When the file cannot be adopted.
    """
    co = _checkout(cwd)
    try:
        result = localmd.adopt(co)
    except localmd.LocalMdError as err:
        fail(err.message, err.details)

    for target in result.relative_imports:
        pp.warning(f"@{target} resolves from the vault copy's folder and will not load")
    if not result.already_linked:
        pp.warning("the hooks commit the vault copy to the vault's git remote")
    emit_value(str(result.target))


@app.command(
    "link",
    help="Link this checkout to the vault's CLAUDE.local.md when that is safe.",
)
def link_command(cwd: Path | None = CWD, as_json: bool = JSON) -> None:  # noqa: FBT001
    """Link this checkout to the vault's CLAUDE.local.md when that is safe.

    Raises:
        Exit: When a file or link is in the way.
    """
    co = _checkout(cwd)
    try:
        result = localmd.link(co)
    except localmd.LocalMdError as err:
        fail(err.message, err.details)

    if as_json:
        emit_json({"status": result.status, "link": str(result.link), "target": str(result.target)})
        if result.status == "conflict":
            raise typer.Exit(1)
        return

    if result.status == "conflict":
        fail(
            f"{result.link} is in the way of the vault copy ({result.state})",
            ["Run `local adopt` to move it into the vault, or `local status` to compare them."],
        )
    if result.status == "none":
        pp.info("the vault holds no CLAUDE.local.md for this project")
        return
    emit_value(str(result.link))


@app.command("status", help="Report how this checkout's CLAUDE.local.md relates to the vault.")
def status_command(cwd: Path | None = CWD, as_json: bool = JSON) -> None:  # noqa: FBT001
    """Report how this checkout's CLAUDE.local.md relates to the vault."""
    co = _checkout(cwd)
    try:
        current = localmd.state(co)
        match = localmd.contents_match(co)
        ignored = co.ignored()
        tracked = co.tracked()
    except localmd.LocalMdError as err:
        fail(err.message, err.details)

    if as_json:
        emit_json(
            {
                "state": str(current),
                "link": str(co.link),
                "target": str(co.target),
                "ignored": ignored,
                "tracked": tracked,
                "match": match,
            }
        )
        return

    emit_value(f"state: {current}")
    emit_value(f"link: {co.link}")
    emit_value(f"target: {co.target}")
    emit_value(f"ignored: {'yes' if ignored else 'no'}")
    emit_value(f"tracked: {'yes' if tracked else 'no'}")
    if current is localmd.State.DIVERGED:
        emit_value(f"contents: {'identical' if match else 'different'}")
