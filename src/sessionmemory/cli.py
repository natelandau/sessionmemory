"""Session memory CLI invoked as `sessionmemory` by a user."""

from __future__ import annotations

import importlib.metadata

import typer

from sessionmemory.commands import delete as delete_commands
from sessionmemory.commands import doctor as doctor_commands
from sessionmemory.commands import export as export_commands
from sessionmemory.commands import init as init_commands
from sessionmemory.commands import inject as inject_commands
from sessionmemory.commands import local as local_commands
from sessionmemory.commands import log as log_commands
from sessionmemory.commands import new as new_commands
from sessionmemory.commands import path as path_commands
from sessionmemory.commands import project
from sessionmemory.commands import reconcile as reconcile_commands
from sessionmemory.commands import reindex as reindex_commands
from sessionmemory.commands import search as search_commands
from sessionmemory.commands._common import VersionedGroup, emit_value

app = typer.Typer(
    cls=VersionedGroup,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

# Panels order the top-level help by who runs a command, so a newcomer reads the
# everyday commands first and the plugin's plumbing last.
EVERYDAY = "Everyday"
SETUP = "Setup"
MAINTENANCE = "Maintenance"
PLUGIN = "Plugin internals"


def _print_version(value: bool) -> None:  # noqa: FBT001
    """Print the installed version bare and exit, for a caller that compares it."""
    if value:
        emit_value(importlib.metadata.version("sessionmemory"))
        raise typer.Exit


@app.callback()
def _root(
    version: bool = typer.Option(  # noqa: FBT001
        False,  # noqa: FBT003
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Print the version and exit.",
    ),
) -> None:
    """Manage a project's memory in a vault of markdown pages."""


app.command(
    "search", help="Search learnings by meaning, or logs or specs.", rich_help_panel=EVERYDAY
)(search_commands.search_command)
app.add_typer(new_commands.app, name="new", rich_help_panel=EVERYDAY)
app.command("path", help="Print this project's vault paths.", rich_help_panel=EVERYDAY)(
    path_commands.path_command
)
app.command("delete", help="Delete files from the vault permanently.", rich_help_panel=EVERYDAY)(
    delete_commands.delete_command
)
app.command("init", help="Create the files a new vault needs.", rich_help_panel=SETUP)(
    init_commands.init
)
app.add_typer(project.app, name="project", rich_help_panel=SETUP)
app.add_typer(local_commands.app, name="local", rich_help_panel=SETUP)
app.command(
    "doctor", help="Report pages, projects, and indexes worth a look.", rich_help_panel=MAINTENANCE
)(doctor_commands.doctor_command)
app.command(
    "reindex",
    help="Rebuild this project's search indexes, or every project's.",
    rich_help_panel=MAINTENANCE,
)(reindex_commands.reindex_command)
app.command(
    "reconcile",
    help="Update this project's session logs to match the commits on its default branch.",
    rich_help_panel=MAINTENANCE,
)(reconcile_commands.reconcile_command)
app.command(
    "export",
    help="Write this project's learnings or logs as a .memoryfield.zip.",
    rich_help_panel=MAINTENANCE,
)(export_commands.export_command)
app.command("inject", help="Print the block a session starts with.", rich_help_panel=PLUGIN)(
    inject_commands.inject_command
)
app.command(
    "log", help="Write this session's log page, replacing its body.", rich_help_panel=PLUGIN
)(log_commands.log_command)


def main() -> None:
    """Run the CLI. This is the console script entry point."""
    app()
