"""Commands that create a page, a document, or a backlog item and print what was written."""

from __future__ import annotations

import enum
from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    CWD_OPTION,
    VersionedGroup,
    emit_json,
    emit_value,
    fail,
    require_project,
    require_vault,
    resolve_body,
)
from sessionmemory.lib import backlog, field, paths, specs
from sessionmemory.lib.config import now, today

app = typer.Typer(
    cls=VersionedGroup,
    no_args_is_help=True,
    help="Create a learning, spec, plan, runbook, reference doc, or backlog item.",
)

# Typer lists an Enum's members in --help and rejects anything else at parse time, so the
# allowed sets are spelled once, in lib/backlog, and mirrored here as choices.
Kind = enum.Enum("Kind", {kind: kind for kind in backlog.KINDS}, type=str)
Size = enum.Enum("Size", {size: size for size in backlog.SIZES}, type=str)

TITLE = typer.Option(..., "--title", help="The title.")
SUMMARY = typer.Option(..., "--summary", help="One sentence a search result shows.")
SPEC_SUMMARY = typer.Option(
    None, "--summary", help="One sentence a search result shows. Defaults to the opening paragraph."
)
BODY = typer.Option("", "--body", help="Markdown body.")
BODY_FILE = typer.Option(None, "--body-file", help="Read the body from a file, or stdin for '-'.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003
KIND = typer.Option(
    ...,
    "--kind",
    help="The commit type heading the item goes under, or decide for an open question.",
)
SIZE = typer.Option(..., "--size", help="Effort: S, M, or L.")
DESCRIPTION = typer.Option(..., "--title", help="The imperative description of the work.")
SUPERSEDES = typer.Option(
    None, "--supersedes", help="The spec this one replaces, by name; it is marked superseded."
)
TOPIC = typer.Option(None, "--topic", help="A topic tag, without the leading hash.")


def _report(path: Path, *, as_json: bool, uuid: str | None = None) -> None:
    if as_json:
        payload: dict[str, str] = {"path": str(path), "title": field.read_page(path).title}
        if uuid is not None:
            payload["uuid"] = uuid
        emit_json(payload)
        return
    emit_value(str(path))


@app.command("learning")
def new_learning(
    title: str = TITLE,
    summary: str = SUMMARY,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    cwd: Path | None = CWD_OPTION,
    *,
    as_json: bool = JSON,
) -> None:
    """Create a learning: one fact this project should remember."""
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


def _new_living_document(folder: Path, title: str, body: str, *, as_json: bool) -> None:
    try:
        path = field.new_living_document(folder, title=title, body=body, now=now())
    except field.PageError as error:
        fail(str(error))
    _report(path, as_json=as_json)


@app.command("spec", help="Create a spec for this project, optionally replacing an older one.")
def new_spec(
    title: str = TITLE,
    summary: str | None = SPEC_SUMMARY,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    supersedes: str | None = SUPERSEDES,
    cwd: Path | None = CWD_OPTION,
    *,
    as_json: bool = JSON,
) -> None:
    """Create a spec, and mark the one it replaces superseded.

    The new spec is reported only once the old one is marked, so a printed path always
    means both writes landed.

    Raises:
        typer.Exit: The named spec is missing or unreadable, or either spec cannot be written.
    """
    body = resolve_body(body, body_file)
    vault = require_vault()
    slug = require_project(vault, cwd)
    folder = paths.specs_dir(vault, slug)
    old: Path | None = None
    if supersedes is not None:
        try:
            old = specs.resolve_name(folder, supersedes)
            specs.load_for_edit(old)
        except specs.SpecError as error:
            fail(str(error))
    try:
        path = field.new_document(
            folder,
            title=title,
            body=body,
            now=now(),
            day=today(),
            status="draft",
            summary=summary,
        )
    except field.PageError as error:
        fail(str(error))
    if old is not None:
        try:
            specs.mark_superseded(old, by=path.name)
        except (specs.SpecError, field.PageError, OSError) as error:
            path.unlink(missing_ok=True)
            fail(f"could not mark {old.name} superseded: {error}", [f"removed {path.name}"])
    _report(path, as_json=as_json)


@app.command("plan")
def new_plan(
    title: str = TITLE,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    cwd: Path | None = CWD_OPTION,
    *,
    as_json: bool = JSON,
) -> None:
    """Create a plan for this project."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    _new_document(
        paths.plans_dir(vault, slug), title, resolve_body(body, body_file), as_json=as_json
    )


@app.command("runbook")
def new_runbook(
    title: str = TITLE,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    cwd: Path | None = CWD_OPTION,
    *,
    as_json: bool = JSON,
) -> None:
    """Create a runbook for this project."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    _new_living_document(
        paths.runbooks_dir(vault, slug), title, resolve_body(body, body_file), as_json=as_json
    )


@app.command("reference")
def new_reference(
    title: str = TITLE,
    body: str = BODY,
    body_file: Path | None = BODY_FILE,
    cwd: Path | None = CWD_OPTION,
    *,
    as_json: bool = JSON,
) -> None:
    """Create a reference doc for this project."""
    vault = require_vault()
    slug = require_project(vault, cwd)
    _new_living_document(
        paths.reference_dir(vault, slug), title, resolve_body(body, body_file), as_json=as_json
    )


def _warn_about_shape(*, in_use: list[str], title: str, topic: str | None) -> None:
    """Warn, without blocking the write, about a new topic tag or an overlong description.

    `in_use` is read before the write, since the line just written always carries its own
    topic.
    """
    if topic is not None:
        tag = topic.strip().removeprefix("#")
        if tag and in_use and tag not in in_use:
            pp.warning(
                f"#{tag} is a new topic in this backlog",
                details=[f"in use: {', '.join(in_use)}"],
            )
    length = len(title.strip())
    if length > backlog.LONG_DESCRIPTION:
        pp.warning(
            f"the description is {length} characters, over the {backlog.LONG_DESCRIPTION} limit",
            details=["keep a line to one sentence and put detail in a spec"],
        )


@app.command("backlog", help="Add one open item to this project's backlog.md.")
def new_backlog(
    kind: Kind = KIND,
    size: Size = SIZE,
    title: str = DESCRIPTION,
    topic: str | None = TOPIC,
    cwd: Path | None = CWD_OPTION,
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
    in_use = backlog.topics(path)
    try:
        line = backlog.add_item(
            path, kind=kind.value, size=size.value, description=title, topic=topic, today=today()
        )
    except backlog.BacklogError as error:
        fail(str(error))
    _warn_about_shape(in_use=in_use, title=title, topic=topic)
    if as_json:
        emit_json({"path": str(path), "line": line})
        return
    emit_value(f"{line}\n{path}")
