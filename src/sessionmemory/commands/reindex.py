"""The `reindex` command: rebuild a project's field indexes from its pages."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    build_embedder,
    emit_json,
    require_project,
    require_vault,
)
from sessionmemory.lib import fieldindex, paths

CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def reindex_command(cwd: Path | None = CWD, *, as_json: bool = JSON) -> None:
    """Bring this project's learnings and logs indexes up to date."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    embedder = build_embedder()
    fields = {
        "learnings": paths.learnings_dir(vault, slug),
        "logs": paths.logs_dir(vault, slug),
    }
    if as_json:
        emit_json(
            {
                name: asdict(fieldindex.refresh(directory, embedder))
                for name, directory in fields.items()
            }
        )
        return
    with pp.step("reindexing") as step:
        for name, directory in fields.items():
            result = fieldindex.refresh(directory, embedder)
            step.sub(
                f"{name}: {result.added} added, {result.updated} updated, "
                f"{result.removed} removed, {result.unchanged} unchanged"
            )
