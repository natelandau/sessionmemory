"""Command that prints one of a project's vault paths."""

from __future__ import annotations

import enum
from pathlib import Path  # noqa: TC003

import typer

from sessionmemory.commands._common import (
    CWD_OPTION,
    JSON_OPTION,
    emit_json,
    emit_value,
    require_project,
    require_vault,
)
from sessionmemory.lib import paths


def _kind(key: str) -> str:
    return "project" if key == "project_dir" else key


# Typer lists an Enum's members in --help and rejects anything else at parse time. The
# members are the keys of `paths.project_paths`, with `project` standing for
# `project_dir`.
PathKind = enum.Enum(
    "PathKind",
    {_kind(key): _kind(key) for key in paths.PROJECT_PATH_KEYS},
    type=str,
)

KIND_ARGUMENT = typer.Argument(None, help="Which path to print. Omit to print them all.")


def path_command(
    kind: PathKind | None = KIND_ARGUMENT,
    cwd: Path | None = CWD_OPTION,
    *,
    as_json: bool = JSON_OPTION,
) -> None:
    """Print one of this project's vault paths, or every one by kind."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    by_kind = {_kind(key): value for key, value in paths.project_paths(vault, slug).items()}
    if kind is not None:
        by_kind = {kind.value: by_kind[kind.value]}
    if as_json:
        emit_json(by_kind)
    elif kind is not None:
        emit_value(by_kind[kind.value])
    else:
        width = max(len(name) for name in by_kind)
        emit_value("\n".join(f"{name:<{width}}  {value}" for name, value in by_kind.items()))
