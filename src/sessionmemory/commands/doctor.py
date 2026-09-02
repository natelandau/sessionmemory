"""The `doctor` command: report what is off, never fail over it."""

from __future__ import annotations

import typer
from nclutils import pp

from sessionmemory.commands._common import build_embedder, emit_json, require_vault
from sessionmemory.lib import doctor

JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def doctor_command(*, as_json: bool = JSON) -> None:
    """Report pages, projects, and indexes worth a look. Always exits 0."""
    vault = require_vault()
    findings = doctor.run(vault, build_embedder())
    if as_json:
        emit_json([{"check": f.check, "path": f.path, "message": f.message} for f in findings])
        return
    if not findings:
        pp.success("nothing to report")
        return
    pp.info(
        f"{len(findings)} suggestion{'s' if len(findings) != 1 else ''}",
        details=[
            f"{f.check}: {f.path}: {f.message}" if f.path else f"{f.check}: {f.message}"
            for f in findings
        ],
    )
