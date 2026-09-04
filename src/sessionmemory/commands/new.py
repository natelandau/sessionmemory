"""Commands that create a page, a document, or a backlog item and print what was written."""

from __future__ import annotations

import enum
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
from sessionmemory.lib import backlog, field, paths
from sessionmemory.lib.config import now, today

app = typer.Typer(no_args_is_help=True, help="Create a learning, spec, plan, or backlog item.")

# Typer lists an Enum's members in --help and rejects anything else at parse time, so the
# allowed sets are spelled once, in lib/backlog, and mirrored here as choices.
Kind = enum.Enum("Kind", {kind: kind for kind in backlog.KINDS}, type=str)
Size = enum.Enum("Size", {size: size for size in backlog.SIZES}, type=str)

TITLE = typer.Option(..., "--title", help="The title.")
SUMMARY = typer.Option(..., "--summary", help="One sentence a search result shows.")
BODY = typer.Option("", "--body", help="Markdown body.")
BODY_FILE = typer.Option(None, "--body-file", help="Read the body from a file, or stdin for '-'.")
CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003
KIND = typer.Option(..., "--kind", help="The commit type heading the item goes under.")
SIZE = typer.Option(..., "--size", help="Effort: S, M, or L.")
DESCRIPTION = typer.Option(..., "--title", help="The imperative description of the work.")
TOPIC = typer.Option(None, "--topic", help="A topic tag, without the leading hash.")


def _report(path: Path, *, as_json: bool, uuid: str | None = None) -> None:
    if as_json:
        payload: dict[str, str] = {"path": str(path), "title": field.read_page(path).title}
        if uuid is not None:
            payload["uuid"] = uuid
        emit_json(payload)
        return
    pp.success(f"created {path.name}", details=[str(path)])


@app.command("learning")
def new_learning(
    title: str = TITLE,
    summary: str = SUMMARY,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    cwd: Path | None = CWD,
    *,
    as_json: bool = JSON,
) -> None:
    """Create a memory page in this project's learnings field."""
    body = resolve_body(body, body_file)
    vault = require_vault()
    slug = require_project(vault, cwd)
    try:
        path = field.new_page(
            paths.learnings_dir(vault, slug), title=title, summary=summary, body=body, now=now()
        )
    except field.PageError as error:
        fail(str(error))
    _report(path, as_json=as_json, uuid=field.read_page(path).uuid)


def _new_document(folder: Path, title: str, body: str, *, as_json: bool) -> None:
    try:
        path = field.new_document(folder, title=title, body=body, now=now(), day=today())
    except field.PageError as error:
        fail(str(error))
    _report(path, as_json=as_json)


@app.command("spec")
def new_spec(
    title: str = TITLE,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    cwd: Path | None = CWD,
    *,
    as_json: bool = JSON,
) -> None:
    """Create a spec for this project."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    _new_document(
        paths.specs_dir(vault, slug), title, resolve_body(body, body_file), as_json=as_json
    )


@app.command("plan")
def new_plan(
    title: str = TITLE,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    cwd: Path | None = CWD,
    *,
    as_json: bool = JSON,
) -> None:
    """Create a plan for this project."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    _new_document(
        paths.plans_dir(vault, slug), title, resolve_body(body, body_file), as_json=as_json
    )


@app.command("backlog", help="Add one open item to this project's backlog.md.")
def new_backlog(
    kind: Kind = KIND,
    size: Size = SIZE,
    title: str = DESCRIPTION,
    topic: str | None = TOPIC,
    cwd: Path | None = CWD,
    *,
    as_json: bool = JSON,
) -> None:
    """Append an open item under its kind heading, creating the file or heading as needed.

    Raises:
        typer.Exit: The title or topic cannot be written as a well-formed line.
    """
    vault = require_vault()
    slug = require_project(vault, cwd)
    path = paths.backlog_path(vault, slug)
    try:
        line = backlog.add_item(
            path, kind=kind.value, size=size.value, description=title, topic=topic, today=today()
        )
    except backlog.BacklogError as error:
        fail(str(error))
    if as_json:
        emit_json({"path": str(path), "line": line})
        return
    pp.success(f"added to {path.name}", details=[line, str(path)])
