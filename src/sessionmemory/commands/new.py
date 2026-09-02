"""Commands that create a page or a document and print the path to write into."""

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
from sessionmemory.lib import field, paths
from sessionmemory.lib.config import now

app = typer.Typer(no_args_is_help=True, help="Create a learning, spec, or plan.")

TITLE = typer.Option(..., "--title", help="The title.")
SUMMARY = typer.Option(..., "--summary", help="One sentence a search result shows.")
BODY = typer.Option("", "--body", help="Markdown body.")
BODY_FILE = typer.Option(None, "--body-file", help="Read the body from a file, or stdin for '-'.")
CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


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
        path = field.new_document(folder, title=title, body=body, now=now())
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
