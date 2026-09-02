"""The `search` command: the one read the agent cannot do with its own tools."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands._common import (
    build_embedder,
    emit_json,
    emit_value,
    fail,
    require_project,
    require_vault,
)
from sessionmemory.lib import fieldindex, paths

QUERY = typer.Argument(..., help="What to look for, in plain words.")
LOGS = typer.Option(False, "--logs", help="Search past session logs instead of learnings.")  # noqa: FBT003
LIMIT = typer.Option(10, "--limit", help="Maximum number of results.")
MAX_DISTANCE = typer.Option(
    fieldindex.DEFAULT_MAX_DISTANCE,
    "--max-distance",
    min=0.0,
    max=2.0,
    help="Farthest cosine distance that still counts as a hit.",
)
READ = typer.Option(False, "--read", help="Print each hit's whole file under its path.")  # noqa: FBT003
CWD = typer.Option(None, "--cwd", help="Directory to resolve the project from.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def search_command(
    query: str = QUERY,
    *,
    logs: bool = LOGS,
    limit: int = LIMIT,
    max_distance: float = MAX_DISTANCE,
    read: bool = READ,
    cwd: Path | None = CWD,
    as_json: bool = JSON,
) -> None:
    """Find the pages nearest in meaning to the query, nearest first."""
    if limit < 1:
        fail("--limit must be at least 1")
    vault = require_vault()
    slug = require_project(vault, cwd)
    directory = paths.logs_dir(vault, slug) if logs else paths.learnings_dir(vault, slug)
    hits = fieldindex.search(
        directory, build_embedder(), query, limit=limit, max_distance=max_distance
    )

    if as_json:
        payload = []
        for hit in hits:
            entry: dict[str, object] = {
                "path": str(hit.path),
                "title": hit.title,
                "summary": hit.summary,
                "distance": hit.distance,
            }
            if read:
                entry["content"] = _content(hit.path)
            payload.append(entry)
        emit_json(payload)
        return
    if not hits:
        pp.info(
            f"no results within distance {max_distance}; raise --max-distance to see farther pages"
        )
        return
    # A path, a title, a summary, and a page are all things a caller copies or parses,
    # so nothing here may be styled.
    if read:
        emit_value("\n\n".join(f"{hit.path}\n{_content(hit.path).rstrip()}" for hit in hits))
        return
    emit_value("\n\n".join(f"{hit.path}\n  {hit.title}\n  {hit.summary}" for hit in hits))


def _content(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")
