"""A project's session logs: one page per session, replaced whole on every write.

The body is replaced rather than appended to, so a caller sends all of it every time
and repeated calls are idempotent. Logs are a field of their own so they can be
searched on request without diluting the learnings field.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sessionmemory.lib import field, paths
from sessionmemory.lib.ids import slugify, strip_date

if TYPE_CHECKING:
    from pathlib import Path

SESSION_FIELD = "session_id"
TRANSCRIPT_FIELD = "transcript"
URL_FIELD = "session_url"

_ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


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


def log_date(title: str, today: str) -> str:
    """The day a log is filed under: the first date its title names, else `today`.

    A log records a session, and the sweep titles it for the moment the session began,
    so a session that runs past midnight is swept on a day its title does not name.
    Filing it under the title's date keeps the filename from carrying both.
    """
    match = _ISO_DATE.search(title)
    return match.group(0) if match else today


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
    transcript: str = "",
    session_url: str = "",
) -> Upserted:
    """Write this session's log, replacing the page it already has rather than adding one.

    `transcript` and `session_url` are written only when given, and an update never
    clears one the page already carries, since a later sweep of the same session may
    be handed less than the first one was.
    """
    source = {
        key: value
        for key, value in ((TRANSCRIPT_FIELD, transcript), (URL_FIELD, session_url))
        if value
    }
    existing = find_session_log(vault, slug, session_id)
    if existing is not None:
        page = field.read_page(existing)
        meta = dict(page.meta)
        meta.update({"title": title, "summary": summary, "updated": now, **source})
        field.write_page(existing, meta, body)
        return Upserted(path=existing, created=False)

    day = log_date(title, today)
    try:
        stem = f"{day}-{slugify(strip_date(title, day))}"
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
        **source,
    }
    field.write_page(path, meta, body)
    return Upserted(path=path, created=True)
