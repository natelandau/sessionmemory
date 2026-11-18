"""The `reconcile` command: bring a project's vault records in line with its repository."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp
from rich.table import Table
from rich.text import Text

from sessionmemory.commands._common import (
    CWD_OPTION,
    JSON_OPTION,
    count_cell,
    emit_json,
    fail,
    require_registered,
    require_vault,
)
from sessionmemory.lib import reconcile
from sessionmemory.lib.commits import CommitsError

DRY_RUN = typer.Option(
    False,  # noqa: FBT003
    "--dry-run",
    help="Report what would change without writing.",
)


COUNT_STYLES = {
    "replaced": "green",
    "dropped": "red",
    "in_progress": "cyan",
    "unchecked": "yellow",
}


def _report(reports: list[reconcile.Report]) -> Table:
    """Lay out each reconciler's counts as one row of a grid."""
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("reconciler")
    table.add_column("logs changed", justify="right")
    for column in COUNT_STYLES:
        table.add_column(column.replace("_", " "), justify="right")
    for report in reports:
        table.add_row(
            Text(report.name),
            count_cell(len(report.changed), "bold"),
            *(count_cell(getattr(report, column), style) for column, style in COUNT_STYLES.items()),
        )
    return table


def reconcile_command(
    cwd: Path | None = CWD_OPTION, *, dry_run: bool = DRY_RUN, as_json: bool = JSON_OPTION
) -> None:
    """Bring this project's logs in line with what landed on the trunk.

    Raises:
        Exit: When the project has no absolute registered root, the root is not a git
            repository, or git cannot answer.
    """
    vault = require_vault()
    slug, resolved = require_registered(vault, cwd)
    try:
        context = reconcile.Context.for_project(
            vault, slug, resolved.project.root if resolved.project else ""
        )
    except reconcile.ContextError as error:
        fail(error.message, error.details)
    # The spinner stays off the --json path so no escape sequence reaches the payload.
    progress = nullcontext() if as_json else pp.console().status("reconciling")
    try:
        with progress:
            reports = reconcile.run(context, dry_run=dry_run)
    except CommitsError as error:
        fail("git could not answer for this project", [str(error)])

    if as_json:
        emit_json(
            [
                {
                    "reconciler": report.name,
                    "changed": [str(path) for path in report.changed],
                    "replaced": report.replaced,
                    "dropped": report.dropped,
                    "in_progress": report.in_progress,
                    "unchecked": report.unchecked,
                    "notes": list(report.notes),
                    "note_logs": report.notes,
                }
                for report in reports
            ]
        )
        return

    pp.console().print(_report(reports))
    verb = "would change" if dry_run else "changed"
    for report in reports:
        if report.changed:
            pp.info(f"{report.name} {verb}", details=[path.name for path in report.changed])
        for note, logs in report.notes.items():
            pp.warning(f"{report.name}: {note}", details=logs)
    if dry_run:
        pp.dryrun("no log was rewritten")
