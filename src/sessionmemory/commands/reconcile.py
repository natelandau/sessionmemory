"""The `reconcile` command: bring a project's vault records in line with its repository."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    CWD_OPTION,
    JSON_OPTION,
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
    try:
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
                    "notes": report.notes,
                }
                for report in reports
            ]
        )
        return

    prefix = "(dry run) " if dry_run else ""
    with pp.step(f"{prefix}reconciling") as step:
        for report in reports:
            count = len(report.changed)
            step.sub(
                f"{report.name}: {count} log{'s' if count != 1 else ''} changed, {report.replaced} "
                f"replaced, {report.dropped} dropped, {report.in_progress} in progress, "
                f"{report.unchecked} unchecked"
            )
            for note in report.notes:
                step.sub(note)
