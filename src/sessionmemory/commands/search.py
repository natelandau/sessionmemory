"""The `search` command: the one read the agent cannot do with its own tools."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    CWD_OPTION,
    JSON_OPTION,
    build_embedder,
    emit_json,
    emit_value,
    fail,
    require_project,
    require_vault,
)
from sessionmemory.lib import fieldindex, paths, specs

QUERY = typer.Argument(..., help="What to look for, in a few distinctive words.")
LOGS = typer.Option(False, "--logs", help="Search past session logs instead of learnings.")  # noqa: FBT003
SPECS = typer.Option(False, "--specs", help="Search specs instead of learnings.")  # noqa: FBT003
INCLUDE_RETIRED = typer.Option(
    False,  # noqa: FBT003
    "--include-retired",
    help="With --specs, also show superseded and abandoned specs.",
)
LIMIT = typer.Option(10, "--limit", "-n", help="Maximum number of results.")
# Hidden because the defaults were measured against labeled queries; these exist for
# re-measuring them, not for loosening a search that found nothing.
MAX_DISTANCE = typer.Option(
    None,
    "--max-distance",
    min=0.0,
    max=2.0,
    help=(
        "Farthest cosine distance that still counts as a hit. "
        f"Default {fieldindex.DEFAULT_MAX_DISTANCE}, or {fieldindex.SPECS_MAX_DISTANCE} with --specs."
    ),
    show_default=False,
    hidden=True,
)
MIN_MARGIN = typer.Option(
    None,
    "--min-margin",
    min=0.0,
    max=2.0,
    help=(
        "How much nearer than the median page searched a hit must sit. 0 turns this off. "
        f"Default {fieldindex.DEFAULT_MIN_MARGIN}, or {fieldindex.SPECS_MIN_MARGIN} with --specs."
    ),
    show_default=False,
    hidden=True,
)
READ = typer.Option(False, "--read", help="Print each hit's whole file under its path.")  # noqa: FBT003

_NO_RESULTS = {
    "learnings": (
        "no results: nothing in learnings matches; try --specs for designs and decisions, "
        "or --logs for what a past session did"
    ),
    "specs": "no results: no spec matches this query",
    "logs": "no results: nothing recorded matches this query",
}


def search_command(  # noqa: PLR0913
    query: list[str] = QUERY,
    *,
    logs: bool = LOGS,
    specs_flag: bool = SPECS,
    include_retired: bool = INCLUDE_RETIRED,
    limit: int = LIMIT,
    max_distance: float | None = MAX_DISTANCE,
    min_margin: float | None = MIN_MARGIN,
    read: bool = READ,
    cwd: Path | None = CWD_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Find the pages nearest in meaning to the query, nearest first."""
    text = " ".join(query)
    if limit < 1:
        fail("--limit must be at least 1")
    if specs_flag and logs:
        fail("--specs and --logs are mutually exclusive", ["search one at a time"])
    if include_retired and not specs_flag:
        fail("--include-retired needs --specs")
    vault = require_vault()
    slug = require_project(vault, cwd)
    field_name = _field_name(specs_flag=specs_flag, logs=logs)
    hits = fieldindex.search(
        paths.project_dir(vault, slug) / field_name,
        build_embedder(),
        text,
        limit=limit,
        max_distance=max_distance,
        min_margin=min_margin,
        exclude=specs.is_retired if specs_flag and not include_retired else None,
    )

    if as_json:
        payload = [_entry(hit, specs_flag=specs_flag, read=read) for hit in hits]
        emit_json(payload)
        return
    if not hits:
        pp.info(_NO_RESULTS[field_name])
        return
    # A path, a title, a summary, and a page are all things a caller copies or parses,
    # so nothing here may be styled.
    if read:
        emit_value("\n\n".join(f"{hit.path}\n{_content(hit.path).rstrip()}" for hit in hits))
        return
    emit_value(
        "\n\n".join(
            f"{hit.path}\n  {_title_line(hit, specs_flag=specs_flag)}\n  {hit.summary}"
            for hit in hits
        )
    )


def _entry(hit: fieldindex.Hit, *, specs_flag: bool, read: bool) -> dict[str, object]:
    entry: dict[str, object] = {
        "path": str(hit.path),
        "title": hit.title,
        "summary": hit.summary,
        "distance": hit.distance,
    }
    if specs_flag:
        entry["status"] = hit.status
    if read:
        entry["content"] = _content(hit.path)
    return entry


def _field_name(*, specs_flag: bool, logs: bool) -> str:
    if specs_flag:
        return paths.SPECS_DIR
    return paths.LOGS_DIR if logs else paths.LEARNINGS_DIR


def _title_line(hit: fieldindex.Hit, *, specs_flag: bool) -> str:
    if specs_flag and hit.status in specs.RETIRED:
        return f"{hit.title} ({hit.status})"
    return hit.title


def _content(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")
