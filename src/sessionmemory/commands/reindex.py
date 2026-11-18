"""The `reindex` command: rebuild a project's indexes from its pages."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp
from rich.table import Table
from rich.text import Text

from sessionmemory.commands._common import (
    CWD_OPTION,
    JSON_OPTION,
    build_embedder,
    count_cell,
    emit_json,
    fail,
    require_project,
    require_vault,
)
from sessionmemory.lib import fieldindex, paths

ALL = typer.Option(False, "--all", help="Reindex every project in the vault.")  # noqa: FBT003

COUNT_STYLES = {"added": "green", "updated": "yellow", "removed": "red", "unchanged": ""}


def _changed(counts: fieldindex.Refresh) -> bool:
    """Return whether a refresh added, updated, or removed any page."""
    return bool(counts.added or counts.updated or counts.removed)


def _report(rows: list[tuple[str, str, fieldindex.Refresh]], *, show_project: bool) -> Table:
    """Lay out each field's counts as a grid, with column totals in the footer.

    Args:
        rows: One `(project, field, counts)` triple per field, in display order.
        show_project: Whether to lead with a project column, naming each project once.

    Returns:
        The table to print.
    """
    table = Table(box=None, pad_edge=False, header_style="bold", show_footer=True)
    if show_project:
        table.add_column("project", footer="total")
    table.add_column("field", footer="" if show_project else "total")
    for column, style in COUNT_STYLES.items():
        total = sum(getattr(counts, column) for *_, counts in rows)
        table.add_column(column, justify="right", footer=count_cell(total, style))

    previous = None
    for project, field, counts in rows:
        cells = [count_cell(getattr(counts, c), style) for c, style in COUNT_STYLES.items()]
        lead = [Text(project if project != previous else "")] if show_project else []
        table.add_row(*lead, Text(field), *cells)
        previous = project
    return table


def _print_vault_report(rows: list[tuple[str, str, fieldindex.Refresh]]) -> None:
    """Print the grid for the projects a refresh changed, then count the rest.

    A large vault's full grid outgrows the terminal, so a project with nothing to refresh
    is only counted.

    Args:
        rows: One `(project, field, counts)` triple per field, in display order.
    """
    changed = {slug for slug, _, counts in rows if _changed(counts)}
    current = len({slug for slug, *_ in rows} - changed)
    if changed:
        pp.console().print(_report([r for r in rows if r[0] in changed], show_project=True))
    if current:
        noun = "project" if current == 1 else "projects"
        prefix = "all " if not changed and current > 1 else ""
        pp.info(f"{prefix}{current} {noun} already up to date", style="default")


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

    if not fields:
        pp.info("no fields to reindex")
        return

    rows = []
    with pp.console().status("reindexing") as status:
        for slug, name, directory in fields:
            status.update(Text(f"reindexing {slug}/{name}"))
            rows.append((slug, name, fieldindex.refresh(directory, embedder)))
    if all_projects:
        _print_vault_report(rows)
    else:
        pp.console().print(_report(rows, show_project=False))
