"""Command that prints one of a project's vault paths."""

from __future__ import annotations

import enum
from pathlib import Path  # noqa: TC003

import typer

from sessionmemory.commands._common import emit_value, require_project, require_vault
from sessionmemory.lib import paths

# Typer lists an Enum's members in --help and rejects anything else at parse time. The
# members are the keys of `paths.project_paths`, with `project` standing for `project_dir`
# and `local` for `local_md`.
PathKind = enum.Enum(
    "PathKind",
    {
        name: name
        for name in (
            "project",
            "learnings",
            "logs",
            "specs",
            "plans",
            "runbooks",
            "reference",
            "backlog",
            "local",
        )
    },
    type=str,
)

KIND_ARGUMENT = typer.Argument(..., help="Which path to print.")
CWD_OPTION = typer.Option(None, "--cwd", help="Directory to resolve the project from.")


def path_command(kind: PathKind = KIND_ARGUMENT, cwd: Path | None = CWD_OPTION) -> None:
    """Print one of this project's vault paths."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    key = {"project": "project_dir", "local": "local_md"}.get(kind.value, kind.value)
    emit_value(paths.project_paths(vault, slug)[key])
