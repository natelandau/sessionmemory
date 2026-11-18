"""The `doctor` command: report what is off, never fail over it."""

from __future__ import annotations

from itertools import groupby
from typing import TYPE_CHECKING

from nclutils import pp
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from sessionmemory.commands._common import JSON_OPTION, build_embedder, emit_json, require_vault
from sessionmemory.lib import doctor

if TYPE_CHECKING:
    from pathlib import Path

VAULT_GROUP = "(vault)"


def _print_report(vault: Path, findings: list[doctor.Finding]) -> None:
    """Print findings grouped under a heading per project, each with its message below its path.

    The message sits on its own line rather than in a column, so a long path never squeezes
    it and nothing is cut at a narrow terminal. Vault findings come first, then projects by
    slug, each keeping the checks' order.
    """
    console = pp.console()
    check_width = max(len(finding.check) for finding in findings)
    ordered = sorted(findings, key=lambda finding: finding.project)
    for project, group in groupby(ordered, key=lambda finding: finding.project):
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="cyan", no_wrap=True, min_width=check_width)
        grid.add_column(overflow="fold")
        for finding in group:
            path = doctor.short_path(vault, finding)
            cell = (
                Text.assemble((path, "bold"), "\n", finding.message)
                if path
                else Text(finding.message)
            )
            grid.add_row(Text(finding.check), cell)
        console.print(Text(project or VAULT_GROUP, style="bold underline"))
        console.print(Padding(grid, (0, 0, 0, 2), expand=False))


def doctor_command(*, as_json: bool = JSON_OPTION) -> None:
    """Report pages, projects, and indexes worth a look. Always exits 0."""
    vault = require_vault()
    findings = doctor.run(vault, build_embedder())
    if as_json:
        emit_json(
            [
                {"check": f.check, "path": f.path, "message": f.message, "project": f.project}
                for f in findings
            ]
        )
        return
    if not findings:
        pp.success("nothing to report")
        return
    pp.info(f"{len(findings)} suggestion{'s' if len(findings) != 1 else ''}")
    _print_report(vault, findings)
