"""The six things that can be wrong with a vault, stated without severity.

Every check is a suggestion. The article's argument holds: an irrelevant or imperfect
page is never surfaced by semantic search, so nothing here fails a build. There is no
repair, because repair is what grew to three hundred lines last time.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sessionmemory.lib import field, fieldindex, paths, registry
from sessionmemory.lib.frontmatter import (
    FrontmatterError,
    MissingFrontmatterError,
    parse,
    unquoted_datetime_keys,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sessionmemory.lib.embed import Embedder


@dataclass(frozen=True)
class Finding:
    """One thing a check found."""

    check: str
    path: str
    message: str


def _fields(vault: Path) -> list[Path]:
    return [
        paths.project_dir(vault, slug) / name
        for slug in paths.iter_project_slugs(vault)
        for name in paths.FIELD_DIRS
        if (paths.project_dir(vault, slug) / name).is_dir()
    ]


def nonconformant_names(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a markdown file in a field whose name breaks the spec's filename rule."""
    return [
        Finding("filename", str(path), "not lowercase ascii letters, digits, and hyphens")
        for directory in _fields(vault)
        for path in sorted(directory.glob("*.md"))
        if path.is_file() and not field.is_debris(path.name) and not field.is_page_name(path.name)
    ]


def oversized_pages(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a page over the 8KB limit; only its first 8KB is embedded."""
    return [
        Finding(
            "size",
            str(path),
            f"{path.stat().st_size} bytes; split it, the limit is {field.PAGE_LIMIT}",
        )
        for directory in _fields(vault)
        for path in field.iter_pages(directory)
        if path.stat().st_size > field.PAGE_LIMIT
    ]


def _blank(value: object) -> bool:
    """Report whether a frontmatter value is missing or an empty/whitespace string."""
    return not (isinstance(value, str) and value.strip())


def malformed_frontmatter(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a page whose frontmatter cannot be parsed, or a learning missing title or summary.

    A logs page with no frontmatter block is fine; a learnings page needs a title and a
    summary to be worth anything a search result shows, so it has nothing to be missing
    when it has no block at all.
    """
    findings = []
    for directory in _fields(vault):
        for path in field.iter_pages(directory):
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                findings.append(Finding("frontmatter", str(path), "not valid UTF-8"))
                continue
            try:
                meta, _body = parse(text)
            except MissingFrontmatterError:
                if path.parent.name == paths.LEARNINGS_DIR:
                    findings.append(Finding("frontmatter", str(path), "missing title or summary"))
                continue
            except FrontmatterError as error:
                findings.append(Finding("frontmatter", str(path), str(error)))
                continue
            if path.parent.name == paths.LEARNINGS_DIR and (
                _blank(meta.get("title")) or _blank(meta.get("summary"))
            ):
                findings.append(Finding("frontmatter", str(path), "missing title or summary"))
    return findings


def unquoted_datetimes(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a page whose frontmatter carries a bare date or datetime.

    The spec requires quoting them: a YAML 1.1 parser types a bare value and a YAML 1.2
    parser leaves it a string, so what the page says would depend on which reads it. A
    block that cannot be parsed at all is left to the frontmatter check.
    """
    findings = []
    for directory in _fields(vault):
        for path in field.iter_pages(directory):
            try:
                keys = unquoted_datetime_keys(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, FrontmatterError):
                continue
            if keys:
                message = f"{', '.join(keys)}: unquoted; quote the value"
                findings.append(Finding("datetime", str(path), message))
    return findings


def dead_projects(vault: Path, _embedder: Embedder) -> list[Finding]:
    """Report a registered project whose repository root no longer exists."""
    try:
        projects = registry.load(vault)
    except registry.RegistryError as error:
        return [Finding("registry", "", str(error))]
    return [
        Finding("project", slug, f"root {project.root} does not exist")
        for slug, project in sorted(projects.items())
        if project.root and not Path(project.root).is_dir()
    ]


def _is_stale(directory: Path, embedder: Embedder) -> str | None:
    index = fieldindex.index_path(directory, embedder)
    if not index.is_file():
        return None
    try:
        conn = fieldindex.connect(index)
    except sqlite3.DatabaseError:
        return "index is not a readable database; run: sessionmemory reindex"
    try:
        stored = {
            row["filename"]: row["sha256_hash"]
            for row in conn.execute("SELECT filename, sha256_hash FROM pages")
        }
    finally:
        conn.close()
    current = {
        path.name: hashlib.sha256(path.read_bytes()).digest()
        for path in field.iter_pages(directory)
    }
    return None if stored == current else "index is behind its pages; run: sessionmemory reindex"


def stale_indexes(vault: Path, embedder: Embedder) -> list[Finding]:
    """Report a field whose index file is unreadable or behind its pages."""
    findings = []
    for directory in _fields(vault):
        message = _is_stale(directory, embedder)
        if message:
            findings.append(Finding("index", str(directory), message))
    return findings


CHECKS: tuple[Callable[[Path, Embedder], list[Finding]], ...] = (
    nonconformant_names,
    oversized_pages,
    malformed_frontmatter,
    unquoted_datetimes,
    dead_projects,
    stale_indexes,
)


def run(vault: Path, embedder: Embedder) -> list[Finding]:
    """Run every check and return what they found."""
    return [finding for check in CHECKS for finding in check(vault, embedder)]
