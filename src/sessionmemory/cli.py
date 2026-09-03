"""Session memory CLI invoked as `sessionmemory` by a user."""

from __future__ import annotations

import importlib.metadata

import typer
from nclutils import pp

from sessionmemory.commands import delete as delete_commands
from sessionmemory.commands import doctor as doctor_commands
from sessionmemory.commands import export as export_commands
from sessionmemory.commands import init as init_commands
from sessionmemory.commands import inject as inject_commands
from sessionmemory.commands import log as log_commands
from sessionmemory.commands import new as new_commands
from sessionmemory.commands import project
from sessionmemory.commands import reindex as reindex_commands
from sessionmemory.commands import search as search_commands
from sessionmemory.commands._common import emit_value

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _print_version(value: bool) -> None:  # noqa: FBT001
    """Print the installed version bare and exit, for a caller that compares it."""
    if value:
        emit_value(importlib.metadata.version("sessionmemory"))
        raise typer.Exit


@app.callback()
def _root(
    verbosity: int = typer.Option(
        0, "-v", "--verbose", count=True, help="Increase output verbosity. Repeat for more."
    ),
    version: bool = typer.Option(  # noqa: ARG001, FBT001
        False,  # noqa: FBT003
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Print the version and exit.",
    ),
) -> None:
    """Manage a project's memory in a vault of markdown pages."""
    pp.configure(verbosity=verbosity)


app.command("delete", help="Delete files from the vault permanently.")(
    delete_commands.delete_command
)
app.command("doctor", help="Report pages, projects, and indexes worth a look.")(
    doctor_commands.doctor_command
)
app.command("export", help="Write this project's field as a .memoryfield.zip.")(
    export_commands.export_command
)
app.command("init", help="Create the files a new vault needs.")(init_commands.init)
app.command("inject", help="Print what this project's session should start with.")(
    inject_commands.inject_command
)
app.command("log", help="Record this session's work in one upserted page.")(
    log_commands.log_command
)
app.command("project", help="Report this directory's project, or register it.")(
    project.project_command
)
app.command("reindex", help="Rebuild this project's search indexes.")(
    reindex_commands.reindex_command
)
app.command("search", help="Search this project's learnings by meaning, or its logs with --logs.")(
    search_commands.search_command
)
app.add_typer(new_commands.app, name="new")


def main() -> None:
    """Run the CLI. This is the console script entry point."""
    app()
