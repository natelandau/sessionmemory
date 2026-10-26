"""The `reconcile` command: bring a project's vault records in line with its repository."""

from __future__ import annotations

from pathlib import Path

import typer
from nclutils import pp

from sessionmemory.commands._common import emit_json, fail, require_project, require_vault
from sessionmemory.lib import reconcile, registry
from sessionmemory.lib.commits import CommitsError, Repository

CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
DRY_RUN = typer.Option(
    False,  # noqa: FBT003
    "--dry-run",
    help="Report what would change without writing.",
)
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def reconcile_command(
    cwd: Path | None = CWD, *, dry_run: bool = DRY_RUN, as_json: bool = JSON
) -> None:
    """Bring this project's logs in line with what landed on the trunk.

    Raises:
        Exit: When the project has no absolute registered root, the root is not a git
            repository, or git cannot answer.
    """
    vault = require_vault()
    slug = require_project(vault, cwd)
    registered = registry.load(vault)[slug].root
    # Path("") is the shell's directory, which would silently reconcile the wrong tree.
    if not registered or not Path(registered).is_absolute():
        fail("project has no registered root", [f"registered root: {registered!r}"])
    root = Path(registered)
    if not Repository(root).is_repository():
        fail("project root is not a git repository", [str(root)])

    context = reconcile.Context(vault=vault, slug=slug, root=root)
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
