"""A project's session logs: one page per session, replaced whole on every write.

The body is replaced rather than appended to, so a caller sends all of it every time
and repeated calls are idempotent. Logs are a field of their own so they can be
searched on request without diluting the learnings field.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessionmemory.lib import field, paths
from sessionmemory.lib.ids import slugify, strip_date

if TYPE_CHECKING:
    from pathlib import Path

SESSION_FIELD = "session_id"


@dataclass(frozen=True)
class Upserted:
    """Where the log landed, and whether this call created it."""

    path: Path
    created: bool


def find_session_log(vault: Path, slug: str, session_id: str) -> Path | None:
    """Return the page already recording this session, if any."""
    for path in field.iter_pages(paths.logs_dir(vault, slug)):
        if field.read_page(path).meta.get(SESSION_FIELD) == session_id:
            return path
    return None


def upsert_log(  # noqa: PLR0913
    vault: Path,
    *,
    slug: str,
    session_id: str,
    title: str,
    summary: str,
    body: str,
    now: str,
    today: str,
) -> Upserted:
    """Write this session's log, replacing the page it already has rather than adding one."""
    existing = find_session_log(vault, slug, session_id)
    if existing is not None:
        page = field.read_page(existing)
        meta = dict(page.meta)
        meta.update({"title": title, "summary": summary, "updated": now})
        field.write_page(existing, meta, body)
        return Upserted(path=existing, created=False)

    try:
        stem = f"{today}-{slugify(strip_date(title, today))}"
    except ValueError as error:
        raise field.PageError(str(error)) from error
    path = field.claim_filename(paths.logs_dir(vault, slug), title, stem=stem)
    meta = {
        "title": title,
        "uuid": str(uuid.uuid4()),
        "summary": summary,
        "created": now,
        "updated": now,
        SESSION_FIELD: session_id,
    }
    field.write_page(path, meta, body)
    return Upserted(path=path, created=True)
