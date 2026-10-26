"""The `inject` command: what a project's session should start with."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer

from sessionmemory.commands._common import emit_json, emit_value, require_project, require_vault
from sessionmemory.lib import inject

CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
COMMAND = typer.Option("sessionmemory", "--command", help="How the guidance names this CLI.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def inject_command(cwd: Path | None = CWD, command: str = COMMAND, *, as_json: bool = JSON) -> None:
    """Print what this project's session should start with."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    injection = inject.build(vault, slug)
    if as_json:
        emit_json(inject.payload(injection, command=command))
        return
    emit_value(inject.render(injection, command=command))
