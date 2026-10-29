"""The `reindex` command: rebuild a project's indexes from its pages."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    CWD_OPTION,
    JSON_OPTION,
    build_embedder,
    emit_json,
    fail,
    require_project,
    require_vault,
)
from sessionmemory.lib import fieldindex, paths

ALL = typer.Option(False, "--all", help="Reindex every project in the vault.")  # noqa: FBT003


def reindex_command(
    cwd: Path | None = CWD_OPTION, *, all_projects: bool = ALL, as_json: bool = JSON_OPTION
) -> None:
    """Bring this project's learnings, logs, and specs indexes up to date, or every project's.

    Raises:
        Exit: When `--all` is combined with `--cwd`.
    """
    vault = require_vault()
    if all_projects:
        if cwd is not None:
            fail("--all and --cwd are mutually exclusive", ["pass one or the other"])
        fields = [
            (directory.parent.name, directory.name, directory)
            for directory in paths.iter_index_dirs(vault)
        ]
    else:
        slug = require_project(vault, cwd)
        fields = [(slug, directory.name, directory) for directory in paths.index_dirs(vault, slug)]
    embedder = build_embedder()

    if as_json:
        payload: dict[str, dict] = {}
        for slug, name, directory in fields:
            counts = asdict(fieldindex.refresh(directory, embedder))
            if all_projects:
                payload.setdefault(slug, {})[name] = counts
            else:
                payload[name] = counts
        emit_json(payload)
        return

    with pp.step("reindexing") as step:
        for slug, name, directory in fields:
            result = fieldindex.refresh(directory, embedder)
            label = f"{slug}/{name}" if all_projects else name
            step.sub(
                f"{label}: {result.added} added, {result.updated} updated, "
                f"{result.removed} removed, {result.unchanged} unchanged"
            )
