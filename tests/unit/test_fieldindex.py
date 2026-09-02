"""Tests for one field's vector index."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from sessionmemory.lib import field, fieldindex
from sessionmemory.lib.embed import StubEmbedder

if TYPE_CHECKING:
    from pathlib import Path

NOW = "2026-09-01T14:03:11Z"


def _page(directory: Path, title: str, body: str = "", summary: str = "s") -> Path:
    return field.new_page(directory, title=title, summary=summary, body=body, now=NOW)


def test_index_path_is_named_for_the_model_code(tmp_path):
    """Verify the index filename begins with the embedder's model code, as the spec requires."""
    assert fieldindex.index_path(tmp_path, StubEmbedder()) == tmp_path / "stub.sqlite3"


def test_refresh_on_missing_field_creates_nothing(tmp_path):
    """Verify a field that does not exist is not created by a read."""
    result = fieldindex.refresh(tmp_path / "absent", StubEmbedder())

    assert result == fieldindex.Refresh(added=0, updated=0, removed=0, unchanged=0)
    assert not (tmp_path / "absent").exists()


def test_refresh_adds_every_page_with_the_spec_columns(tmp_path):
    """Verify each page gets one row holding filename, frontmatter JSON, mtime, sha256, and a vector."""
    # Given two pages
    _page(tmp_path, "Alpha", body="one")
    _page(tmp_path, "Beta", body="two")

    # When refreshing
    result = fieldindex.refresh(tmp_path, StubEmbedder())

    # Then both are rows with the spec's columns
    assert result.added == 2
    conn = sqlite3.connect(fieldindex.index_path(tmp_path, StubEmbedder()))
    rows = conn.execute(
        "SELECT filename, frontmatter, last_modified, sha256_hash, embedding FROM pages ORDER BY filename"
    ).fetchall()
    conn.close()
    assert [row[0] for row in rows] == ["alpha.md", "beta.md"]
    assert json.loads(rows[0][1])["title"] == "Alpha"
    assert rows[0][2].endswith("Z")
    assert len(rows[0][3]) == 32
    assert len(rows[0][4]) == 4 * StubEmbedder().dim


def test_refresh_reembeds_only_what_changed(tmp_path):
    """Verify an unchanged page is skipped, an edited one is updated, a deleted one is removed."""
    # Given an indexed field
    alpha = _page(tmp_path, "Alpha", body="one")
    beta = _page(tmp_path, "Beta", body="two")
    fieldindex.refresh(tmp_path, StubEmbedder())

    # When one page changes and one disappears
    page = field.read_page(alpha)
    field.write_page(alpha, page.meta, "one, edited")
    beta.unlink()

    # Then the refresh reports exactly that
    result = fieldindex.refresh(tmp_path, StubEmbedder())
    assert result == fieldindex.Refresh(added=0, updated=1, removed=1, unchanged=0)


def test_refresh_ignores_the_index_file_and_debris(tmp_path):
    """Verify the sqlite file beside the pages is never treated as a page."""
    _page(tmp_path, "Alpha")
    fieldindex.refresh(tmp_path, StubEmbedder())
    (tmp_path / ".DS_Store").write_text("")

    result = fieldindex.refresh(tmp_path, StubEmbedder())

    assert result.unchanged == 1
    assert result.added == 0


def test_search_returns_nearest_first_with_title_and_summary(tmp_path):
    """Verify a search over a fresh field ranks by cosine distance and carries display fields."""
    # Given pages, one of which the stub embeds identically to the query text
    _page(tmp_path, "Match", summary="the match", body="")
    _page(tmp_path, "Other", summary="other", body="")
    matching_text = (tmp_path / "match.md").read_text(encoding="utf-8")

    # When searching with that exact text
    hits = fieldindex.search(tmp_path, StubEmbedder(), matching_text, limit=5, max_distance=2.0)

    # Then it is first, with its display fields, and the distance is bounded
    assert [hit.path.name for hit in hits] == ["match.md", "other.md"]
    assert hits[0].title == "Match"
    assert hits[0].summary == "the match"
    assert 0.0 <= hits[0].distance <= 2.0


def test_search_refreshes_before_querying(tmp_path):
    """Verify a page written moments ago is found without an explicit reindex."""
    _page(tmp_path, "Fresh")

    hits = fieldindex.search(tmp_path, StubEmbedder(), "anything", limit=5, max_distance=2.0)

    assert [hit.path.name for hit in hits] == ["fresh.md"]


def test_search_missing_field_is_empty(tmp_path):
    """Verify searching a project with no field yet returns nothing and creates nothing."""
    assert fieldindex.search(tmp_path / "absent", StubEmbedder(), "q", limit=5) == []
    assert not (tmp_path / "absent").exists()


def test_search_honors_limit(tmp_path):
    """Verify no more than limit hits come back."""
    for name in ("a", "b", "c"):
        _page(tmp_path, name)

    assert len(fieldindex.search(tmp_path, StubEmbedder(), "q", limit=2, max_distance=2.0)) == 2


def test_forget_removes_one_row(tmp_path):
    """Verify a deleted page's row can be dropped eagerly."""
    _page(tmp_path, "Alpha")
    fieldindex.refresh(tmp_path, StubEmbedder())

    fieldindex.forget(tmp_path, StubEmbedder(), "alpha.md")

    conn = sqlite3.connect(fieldindex.index_path(tmp_path, StubEmbedder()))
    assert conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
    conn.close()


def test_forget_without_an_index_is_a_noop(tmp_path):
    """Verify forgetting from a field that was never indexed creates no index."""
    fieldindex.forget(tmp_path, StubEmbedder(), "alpha.md")
    assert not fieldindex.index_path(tmp_path, StubEmbedder()).exists()


def test_embedding_input_truncates_to_the_page_limit_on_a_character_boundary():
    """Verify the embedding input is at most PAGE_LIMIT bytes and never splits a character."""
    text = "é" * 5000  # 2 bytes each, 10000 bytes

    truncated = fieldindex.embedding_input(text)

    assert len(truncated.encode("utf-8")) <= field.PAGE_LIMIT
    assert truncated == "é" * 4096


def test_refresh_indexes_a_page_with_invalid_utf8_bytes(tmp_path):
    """Verify a page saved with invalid UTF-8 is indexed rather than crashing the refresh."""
    page = tmp_path / "invalid.md"
    page.write_bytes(b"---\ntitle: broken\nsummary: s\n---\n\xff\xfebody\n")

    result = fieldindex.refresh(tmp_path, StubEmbedder())

    assert result.added == 1
    hits = fieldindex.search(tmp_path, StubEmbedder(), "broken", limit=5, max_distance=2.0)
    assert [hit.path.name for hit in hits] == ["invalid.md"]


def test_a_corrupt_index_is_rebuilt(tmp_path):
    """Verify a file that is not a database is discarded and rebuilt rather than crashing a read."""
    _page(tmp_path, "Alpha")
    fieldindex.index_path(tmp_path, StubEmbedder()).write_bytes(b"not a database")

    hits = fieldindex.search(tmp_path, StubEmbedder(), "q", limit=5, max_distance=2.0)

    assert [hit.path.name for hit in hits] == ["alpha.md"]


def test_search_drops_hits_farther_than_max_distance(tmp_path):
    """Verify a page beyond the cutoff is not a hit, however few hits there are."""
    # Given a page the stub embeds identically to the query and one it does not
    _page(tmp_path, "Match", summary="the match", body="")
    _page(tmp_path, "Other", summary="other", body="")
    matching_text = (tmp_path / "match.md").read_text(encoding="utf-8")

    # When searching with a cutoff that only the identical page can clear
    hits = fieldindex.search(tmp_path, StubEmbedder(), matching_text, limit=5, max_distance=0.5)

    # Then the far page is absent rather than padding the result
    assert [hit.path.name for hit in hits] == ["match.md"]


def test_search_default_cutoff_is_the_measured_one(tmp_path):
    """Verify a search with no cutoff named applies the measured default."""
    _page(tmp_path, "Far")

    assert fieldindex.search(tmp_path, StubEmbedder(), "unrelated", limit=5) == []
    assert fieldindex.DEFAULT_MAX_DISTANCE == 0.45
