"""The `log` command: record this session's work in one upserted page."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    emit_json,
    fail,
    require_project,
    require_vault,
    resolve_body,
)
from sessionmemory.lib import field, log
from sessionmemory.lib.config import now, today

SESSION_ID = typer.Option(..., "--session-id", help="This session's identifier.")
TITLE = typer.Option(..., "--title", help="The log's title.")
SUMMARY = typer.Option("", "--summary", help="One sentence a search result shows.")
BODY = typer.Option("", "--body", help="Markdown body. Replaces what is there.")
BODY_FILE = typer.Option(None, "--body-file", help="Read the body from a file, or stdin for '-'.")
TRANSCRIPT = typer.Option("", "--transcript", help="Path to this session's transcript on disk.")
URL = typer.Option("", "--url", help="Where this session can be opened online.")
CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def log_command(  # noqa: PLR0913, PLR0917
    session_id: str = SESSION_ID,
    title: str = TITLE,
    summary: str = SUMMARY,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    transcript: str = TRANSCRIPT,
    url: str = URL,
    cwd: Path | None = CWD,
    *,
    as_json: bool = JSON,
) -> None:
    """Record this session's work in the one page that belongs to it.

    Raises:
        Exit: When the directory is not a registered project or the page cannot be written.
    """
    body = resolve_body(body, body_file)
    vault = require_vault()
    slug = require_project(vault, cwd)
    try:
        result = log.upsert_log(
            vault,
            slug=slug,
            session_id=session_id,
            title=title,
            summary=summary,
            body=body,
            now=now(),
            today=today(),
            transcript=transcript,
            session_url=url,
        )
    except field.PageError as error:
        fail(str(error))
    action = "created" if result.created else "updated"
    if as_json:
        emit_json({"path": str(result.path), "action": action})
        return
    pp.success(f"{action} {result.path.name}", details=[str(result.path)])
