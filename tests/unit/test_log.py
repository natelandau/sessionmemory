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


def test_two_sessions_with_distinct_start_times_get_distinct_stems(tmp_path):
    """Verify a clock time in the title becomes part of the stem, so no suffix is needed."""
    first = _upsert(tmp_path, session_id="one", title="2026-09-01 09:14")
    second = _upsert(tmp_path, session_id="two", title="2026-09-01 15:02")

    assert first.path.name == "2026-09-01-09-14.md"
    assert second.path.name == "2026-09-01-15-02.md"


def test_the_stem_is_dated_for_the_day_the_title_names(tmp_path):
    """Verify a session swept after midnight is filed under the day it began, not the day it was swept."""
    result = _upsert(tmp_path, title="2026-09-01 23:50", today="2026-09-02")

    assert result.path.name == "2026-09-01-23-50.md"


def test_a_date_in_the_title_is_not_written_twice(tmp_path):
    """Verify the filename does not repeat a date the title already carries."""
    result = _upsert(tmp_path, title="2026-09-01 shared-context")

    assert result.path.name == "2026-09-01-shared-context.md"


def test_upsert_records_transcript_and_session_url_when_given(tmp_path):
    """Verify the transcript path and online link land in frontmatter beside the session id."""
    result = _upsert(
        tmp_path,
        transcript="/home/me/.claude/projects/x/abc.jsonl",
        session_url="https://claude.ai/code/session_01ABC",
    )

    meta, _ = parse(result.path.read_text(encoding="utf-8"))
    assert meta["transcript"] == "/home/me/.claude/projects/x/abc.jsonl"
    assert meta["session_url"] == "https://claude.ai/code/session_01ABC"


def test_upsert_omits_empty_transcript_and_session_url(tmp_path):
    """Verify a session with no link writes no empty key, so the page stays at six fields."""
    result = _upsert(tmp_path)

    meta, _ = parse(result.path.read_text(encoding="utf-8"))
    assert "transcript" not in meta
    assert "session_url" not in meta


def test_upsert_keeps_a_recorded_link_when_a_later_call_omits_it(tmp_path):
    """Verify an update never clears a transcript or link a first write recorded."""
    _upsert(tmp_path, transcript="/t/abc.jsonl", session_url="https://claude.ai/code/session_1")

    second = _upsert(tmp_path, body="New body.")

    meta, _ = parse(second.path.read_text(encoding="utf-8"))
    assert meta["transcript"] == "/t/abc.jsonl"
    assert meta["session_url"] == "https://claude.ai/code/session_1"


def test_upsert_fills_a_link_a_first_write_lacked(tmp_path):
    """Verify a session bridged after its first sweep gains the link on the next one."""
    _upsert(tmp_path)

    second = _upsert(tmp_path, session_url="https://claude.ai/code/session_2")

    meta, _ = parse(second.path.read_text(encoding="utf-8"))
    assert meta["session_url"] == "https://claude.ai/code/session_2"
