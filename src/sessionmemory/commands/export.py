"""The `export` command: write a project's field as a .memoryfield.zip."""

from __future__ import annotations

from pathlib import Path

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    build_embedder,
    emit_json,
    fail,
    require_project,
    require_vault,
)
from sessionmemory.lib import export, field, paths

CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
LOGS = typer.Option(False, "--logs", help="Export the logs field instead of learnings.")  # noqa: FBT003
OUTPUT = typer.Option(
    None,
    "--output",
    help="Where to write the zip. Defaults to <slug>.memoryfield.zip in the current directory.",
)
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def export_command(
    cwd: Path | None = CWD,
    *,
    logs: bool = LOGS,
    output: Path | None = OUTPUT,
    as_json: bool = JSON,
) -> None:
    """Write this project's field as a .memoryfield.zip."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    embedder = build_embedder()

    field_directory = paths.logs_dir(vault, slug) if logs else paths.learnings_dir(vault, slug)

    if output is None:
        suffix = "-logs.memoryfield.zip" if logs else ".memoryfield.zip"
        output = Path.cwd() / f"{slug}{suffix}"

    if not as_json and not field_directory.is_dir():
        pp.warning(f"{field_directory} does not exist; the archive holds no pages")

    try:
        result = export.export_field(field_directory, embedder, output)
    except OSError as error:
        fail(f"cannot write {output}: {error}")
    pages = len(field.iter_pages(field_directory))

    if as_json:
        emit_json({"path": str(result), "pages": pages})
        return

    label = "page" if pages == 1 else "pages"
    pp.success(f"exported {pages} {label}", details=[str(result)])
