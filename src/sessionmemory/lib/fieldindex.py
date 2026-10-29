"""One field's vector index: the spec's `pages` table in one SQLite file.

The index is derived from the pages beside it and can be deleted at any time. Freshness
is the sha256 of each file, so a page edited in any editor is re-embedded on the next
read, and every read refreshes first so nobody has to remember `reindex`.

There is no application lock. A page write is an atomic rename, SQLite serializes its
own writers, and a corrupt file is discarded and rebuilt rather than reported.

A field embeds the whole file. A `specs/` directory is indexed the same way, with the
same table and file name, but embeds `specs.embedding_input`, a derived input of the
title and headings, since a spec's body is too long and too uniform to embed whole.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import sqlite_vec

from sessionmemory.lib import field, paths, specs

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sessionmemory.lib.embed import Embedder

# The reference implementation's default cutoff for nomic-embed-text-v1.5. On its own it
# admits most unrelated queries, so it is only a ceiling and the margin below decides.
DEFAULT_MAX_DISTANCE = 0.45

# Measured with nomic-embed-text-v1.5 on 120 labeled queries across four projects' learnings
# and logs: a page that answers the query sits at least this much nearer than the field's
# median page, and the nearest page to an unrelated query does not. The rule is relative
# because phrasing a query as a question lowers every distance at once, and because a log,
# which summarizes a whole session, sits near every query about its project.
DEFAULT_MIN_MARGIN = 0.11

# A median over fewer pages than this is noise, so a small field measures against the
# median background seen across the labeled queries instead.
MIN_BACKGROUND_PAGES = 8
FALLBACK_BACKGROUND = 0.49

# Measured with nomic-embed-text-v1.5 on 50 labeled keyword queries against 51 specs, where
# the learnings defaults returned hits for 3 of 10 queries with no answer. Specs are
# embedded from a derived input rather than the whole page, so they need their own numbers.
SPECS_MAX_DISTANCE = 0.38
SPECS_MIN_MARGIN = 0.07

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    filename      TEXT PRIMARY KEY,
    frontmatter   JSON NOT NULL,
    last_modified DATETIME NOT NULL,
    sha256_hash   BLOB NOT NULL,
    embedding     BLOB NOT NULL
);
"""


@dataclass(frozen=True)
class Hit:
    """One search result, nearest first when ordered by `distance`."""

    path: Path
    title: str
    summary: str
    distance: float
    status: str = ""


@dataclass(frozen=True)
class Refresh:
    """What one refresh changed."""

    added: int
    updated: int
    removed: int
    unchanged: int


def index_path(field_dir: Path, embedder: Embedder) -> Path:
    """Return the index file for one field, named for the model that fills it."""
    return field_dir / f"{embedder.name}.sqlite3"


def connect(path: Path) -> sqlite3.Connection:
    """Open an index file, creating the table if the file is new."""
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)  # noqa: FBT003
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)  # noqa: FBT003
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_SCHEMA)
    except BaseException:
        conn.close()
        raise
    return conn


def _open(path: Path) -> sqlite3.Connection:
    """Open the index, discarding and recreating a file SQLite cannot read."""
    try:
        return connect(path)
    except sqlite3.DatabaseError:
        path.unlink(missing_ok=True)
        path.with_name(f"{path.name}-journal").unlink(missing_ok=True)
        return connect(path)


def embedding_input(text: str) -> str:
    """Return the first `PAGE_LIMIT` bytes of `text`, never splitting a character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= field.PAGE_LIMIT:
        return text
    return encoded[: field.PAGE_LIMIT].decode("utf-8", errors="ignore")


def _is_specs(field_dir: Path) -> bool:
    return field_dir.name == paths.SPECS_DIR


def _embedding_text(field_dir: Path, path: Path, text: str) -> str:
    if _is_specs(field_dir):
        return specs.embedding_input(text, stem=path.stem)
    return embedding_input(text)


def _mtime_iso(path: Path) -> str:
    stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _drop_outdated_spec_vectors(conn: sqlite3.Connection, field_dir: Path) -> None:
    """Empty a specs index built under another input rule, so every spec is re-embedded.

    Freshness is by file hash, so a change to `specs.embedding_input` would otherwise
    leave every unchanged spec on a vector derived by the old rule.
    """
    if not _is_specs(field_dir):
        return
    if conn.execute("PRAGMA user_version").fetchone()[0] != specs.INPUT_VERSION:
        conn.execute("DELETE FROM pages")
        conn.execute(f"PRAGMA user_version = {int(specs.INPUT_VERSION)}")


def _refresh(conn: sqlite3.Connection, field_dir: Path, embedder: Embedder) -> Refresh:
    _drop_outdated_spec_vectors(conn, field_dir)
    stored = {
        row["filename"]: row["sha256_hash"]
        for row in conn.execute("SELECT filename, sha256_hash FROM pages")
    }
    pending: list[tuple[str, str, str, bytes, str]] = []
    unchanged = 0
    seen: set[str] = set()
    for path in field.iter_pages(field_dir):
        seen.add(path.name)
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).digest()
        # Hash the raw bytes so freshness tracks the file as written; a page saved
        # with invalid UTF-8 is still indexed rather than crashing the refresh.
        text = raw.decode("utf-8", errors="replace")
        if stored.get(path.name) == digest:
            unchanged += 1
            continue
        page = field.read_page(path)
        pending.append(
            (
                path.name,
                json.dumps(page.meta),
                _mtime_iso(path),
                digest,
                _embedding_text(field_dir, path, text),
            )
        )

    gone = sorted(set(stored) - seen)
    conn.executemany("DELETE FROM pages WHERE filename = ?", [(name,) for name in gone])

    vectors = embedder.encode_documents([text for *_rest, text in pending])
    conn.executemany(
        "INSERT OR REPLACE INTO pages (filename, frontmatter, last_modified, sha256_hash, embedding)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (name, meta, modified, digest, sqlite_vec.serialize_float32(vector))
            for (name, meta, modified, digest, _text), vector in zip(pending, vectors, strict=True)
        ],
    )
    conn.commit()
    added = sum(1 for name, *_rest in pending if name not in stored)
    return Refresh(
        added=added, updated=len(pending) - added, removed=len(gone), unchanged=unchanged
    )


def refresh(field_dir: Path, embedder: Embedder) -> Refresh:
    """Bring one field's index up to date with the pages beside it."""
    if not field_dir.is_dir():
        return Refresh(added=0, updated=0, removed=0, unchanged=0)
    conn = _open(index_path(field_dir, embedder))
    try:
        return _refresh(conn, field_dir, embedder)
    finally:
        conn.close()


def default_cutoffs(field_dir: Path) -> tuple[float, float]:
    """Return the measured `max_distance` and `min_margin` for searching `field_dir`.

    Args:
        field_dir (Path): The directory searched.

    Returns:
        tuple[float, float]: The distance cutoff and the margin, the specs pair for
            `specs/` and the fields' pair for the rest.
    """
    if _is_specs(field_dir):
        return SPECS_MAX_DISTANCE, SPECS_MIN_MARGIN
    return DEFAULT_MAX_DISTANCE, DEFAULT_MIN_MARGIN


def search(
    field_dir: Path,
    embedder: Embedder,
    query: str,
    *,
    limit: int,
    max_distance: float | None = None,
    min_margin: float | None = None,
    exclude: Callable[[dict[str, Any]], bool] | None = None,
) -> list[Hit]:
    """Return the pages that stand out as nearest to `query`, nearest first, refreshing the index first.

    A hit sits within `max_distance` and at least `min_margin` nearer than the field's
    median page. A cutoff rather than a bare top-k, so a query nothing answers returns
    nothing instead of the nearest pages dressed up as hits. A `min_margin` of 0 leaves
    only the absolute cutoff. Either left as None takes the measured default for the
    directory searched, since specs were measured apart from the fields. `exclude` drops a
    page by its frontmatter after the median is measured, so a dropped page never uses up
    `limit`.
    """
    if not field_dir.is_dir():
        return []
    default_distance, default_margin = default_cutoffs(field_dir)
    max_distance = default_distance if max_distance is None else max_distance
    min_margin = default_margin if min_margin is None else min_margin
    conn = _open(index_path(field_dir, embedder))
    try:
        _refresh(conn, field_dir, embedder)
        rows = conn.execute(
            "SELECT filename, frontmatter, vec_distance_cosine(embedding, ?) AS distance"
            " FROM pages ORDER BY distance",
            (sqlite_vec.serialize_float32(embedder.encode_query(query)),),
        ).fetchall()
    finally:
        conn.close()
    ceiling = max_distance
    if min_margin > 0:
        distances = [float(row["distance"]) for row in rows]
        background = (
            statistics.median(distances)
            if len(distances) >= MIN_BACKGROUND_PAGES
            else FALLBACK_BACKGROUND
        )
        ceiling = min(ceiling, background - min_margin)
    hits = []
    for row in rows:
        if float(row["distance"]) > ceiling or len(hits) == limit:
            break
        meta = json.loads(row["frontmatter"])
        if exclude is not None and exclude(meta):
            continue
        status = meta.get("status")
        path = field_dir / row["filename"]
        if _is_specs(field_dir):
            try:
                text = path.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                # The file went away after the refresh; a hit for it would point at nothing.
                continue
            title = specs.parse_spec(text, stem=path.stem).title
            summary = specs.hit_summary(text)
        else:
            title = meta.get("title")
            summary = meta.get("summary")
        hits.append(
            Hit(
                path=path,
                title=title if isinstance(title, str) else "",
                summary=summary if isinstance(summary, str) else "",
                distance=float(row["distance"]),
                status=status if isinstance(status, str) else "",
            )
        )
    return hits


def forget(field_dir: Path, embedder: Embedder, filename: str) -> None:
    """Drop one page's row, so a deleted page stops matching before the next refresh."""
    path = index_path(field_dir, embedder)
    if not path.is_file():
        return
    conn = _open(path)
    try:
        conn.execute("DELETE FROM pages WHERE filename = ?", (filename,))
        conn.commit()
    finally:
        conn.close()
