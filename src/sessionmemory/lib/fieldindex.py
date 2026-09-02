"""One field's vector index: the spec's `pages` table in one SQLite file.

The index is derived from the pages beside it and can be deleted at any time. Freshness
is the sha256 of each file, so a page edited in any editor is re-embedded on the next
read, and every read refreshes first so nobody has to remember `reindex`.

There is no application lock. A page write is an atomic rename, SQLite serializes its
own writers, and a corrupt file is discarded and rebuilt rather than reported.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

import sqlite_vec

from sessionmemory.lib import field

if TYPE_CHECKING:
    from pathlib import Path

    from sessionmemory.lib.embed import Embedder

# Measured on a real vault with nomic-embed-text-v1.5: a page that answers the query sits
# under 0.25, a related neighbor under 0.40, and the nearest page to an unrelated query
# sits at 0.45 or beyond. It matches the reference implementation's default for the model.
DEFAULT_MAX_DISTANCE = 0.45

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


def _mtime_iso(path: Path) -> str:
    stamp = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.UTC)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _refresh(conn: sqlite3.Connection, field_dir: Path, embedder: Embedder) -> Refresh:
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
        pending.append((path.name, json.dumps(page.meta), _mtime_iso(path), digest, text))

    gone = sorted(set(stored) - seen)
    conn.executemany("DELETE FROM pages WHERE filename = ?", [(name,) for name in gone])

    # The spec's embedding input is the whole file, frontmatter included.
    vectors = embedder.encode_documents([embedding_input(text) for *_rest, text in pending])
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


def search(
    field_dir: Path,
    embedder: Embedder,
    query: str,
    *,
    limit: int,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> list[Hit]:
    """Return the pages within `max_distance` of `query`, nearest first, refreshing the index first.

    A cutoff rather than a bare top-k, so a query nothing answers returns nothing instead
    of the nearest pages dressed up as hits.
    """
    if not field_dir.is_dir():
        return []
    conn = _open(index_path(field_dir, embedder))
    try:
        _refresh(conn, field_dir, embedder)
        rows = conn.execute(
            "SELECT * FROM ("
            " SELECT filename, frontmatter, vec_distance_cosine(embedding, ?) AS distance"
            " FROM pages)"
            " WHERE distance <= ? ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(embedder.encode_query(query)), max_distance, limit),
        ).fetchall()
    finally:
        conn.close()
    hits = []
    for row in rows:
        meta = json.loads(row["frontmatter"])
        title = meta.get("title")
        summary = meta.get("summary")
        hits.append(
            Hit(
                path=field_dir / row["filename"],
                title=title if isinstance(title, str) else "",
                summary=summary if isinstance(summary, str) else "",
                distance=float(row["distance"]),
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
