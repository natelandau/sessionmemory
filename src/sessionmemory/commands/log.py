"""The `log` command: write this session's one log page, replacing its body."""

from __future__ import annotations

import os
from pathlib import Path  # noqa: TC003

import typer

from sessionmemory.commands._common import (
    CWD_OPTION,
    emit_json,
    emit_value,
    fail,
    require_project,
    require_vault,
    resolve_body,
)
from sessionmemory.lib import field, log
from sessionmemory.lib.config import now, today

# The sweep hook sets this name; hooks import nothing from src, so it is defined on both sides.
LOG_TITLE_ENV = "SESSIONMEMORY_LOG_TITLE"
SESSION_ID = typer.Option(..., "--session-id", help="This session's identifier.")
TITLE = typer.Option(
    "", "--title", help=f"The log's title. Required unless {LOG_TITLE_ENV} is set, which wins."
)
SUMMARY = typer.Option("", "--summary", help="One sentence a search result shows.")
BODY = typer.Option("", "--body", help="Markdown body. Replaces what is there.")
BODY_FILE = typer.Option(None, "--body-file", help="Read the body from a file, or stdin for '-'.")
TRANSCRIPT = typer.Option("", "--transcript", help="Path to this session's transcript on disk.")
URL = typer.Option("", "--url", help="Where this session can be opened online.")
JSON = typer.Option(
    False,  # noqa: FBT003
    "--json",
    help="Emit JSON naming the path and whether the page was created or updated.",
)


def log_command(  # noqa: PLR0913, PLR0917
    session_id: str = SESSION_ID,
    title: str = TITLE,
    summary: str = SUMMARY,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    transcript: str = TRANSCRIPT,
    url: str = URL,
    cwd: Path | None = CWD_OPTION,
    *,
    as_json: bool = JSON,
) -> None:
    """Record this session's work in the one page that belongs to it.

    Prints only the page's path, as every `new` command does, so a caller can capture it.

    Raises:
        Exit: When the directory is not a registered project or the page cannot be written.
    """
    title = os.environ.get(LOG_TITLE_ENV, "").strip() or title.strip()
    if not title:
        fail("a log needs --title")
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
    emit_value(str(result.path))
