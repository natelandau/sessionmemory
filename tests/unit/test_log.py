"""Tests for session log upserts."""

from __future__ import annotations

import uuid

from sessionmemory.lib import log
from sessionmemory.lib.frontmatter import parse

NOW = "2026-09-01T14:03:11Z"
TODAY = "2026-09-01"


def _upsert(vault, **overrides) -> log.Upserted:
    kwargs = {
        "slug": "demo",
        "session_id": "abc",
        "title": "Session",
        "summary": "what happened",
        "body": "Body.",
        "now": NOW,
        "today": TODAY,
    }
    kwargs.update(overrides)
    return log.upsert_log(vault, **kwargs)


def test_upsert_creates_a_dated_page_with_session_id(tmp_path):
    """Verify a first write creates projects/demo/logs/<date>-<slug>.md with the six fields."""
    result = _upsert(tmp_path)

    assert result.created is True
    assert result.path == tmp_path / "projects" / "demo" / "logs" / "2026-09-01-session.md"
    meta, body = parse(result.path.read_text(encoding="utf-8"))
    assert list(meta) == ["title", "uuid", "summary", "created", "updated", "session_id"]
    uuid.UUID(meta["uuid"])
    assert meta["session_id"] == "abc"
    assert body == "Body.\n"


def test_upsert_replaces_body_and_keeps_identity(tmp_path):
    """Verify a second write for the same session rewrites the same file whole."""
    first = _upsert(tmp_path)
    first_meta, _ = parse(first.path.read_text(encoding="utf-8"))

    second = _upsert(tmp_path, title="Renamed", body="New body.", now="2026-09-01T15:00:00Z")

    assert second.created is False
    assert second.path == first.path
    meta, body = parse(second.path.read_text(encoding="utf-8"))
    assert meta["uuid"] == first_meta["uuid"]
    assert meta["created"] == NOW
    assert meta["updated"] == "2026-09-01T15:00:00Z"
    assert meta["title"] == "Renamed"
    assert body == "New body.\n"


def test_two_sessions_on_one_day_get_two_files(tmp_path):
    """Verify a second session with the same title takes a numeric suffix."""
    _upsert(tmp_path, session_id="one")
    second = _upsert(tmp_path, session_id="two")

    assert second.path.name == "2026-09-01-session-2.md"


def test_a_date_in_the_title_is_not_written_twice(tmp_path):
    """Verify the filename does not repeat a date the title already carries."""
    result = _upsert(tmp_path, title="2026-09-01 shared-context")

    assert result.path.name == "2026-09-01-shared-context.md"
