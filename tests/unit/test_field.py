"""Tests for the page model."""

from __future__ import annotations

import uuid

import pytest

from sessionmemory.lib import field
from sessionmemory.lib.frontmatter import parse

NOW = "2026-09-01T14:03:11Z"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("carbon-fiber.md", True),
        ("a.md", True),
        ("2026-09-01-session-3.md", True),
        ("Carbon.md", False),
        ("-leading.md", False),
        ("trailing-.md", False),
        ("under_score.md", False),
        ("notes.txt", False),
        (".md", False),
    ],
)
def test_is_page_name(name, expected):
    """Verify the spec's filename rule is applied exactly."""
    assert field.is_page_name(name) is expected


@pytest.mark.parametrize(
    "name", [".DS_Store", "desktop.ini", "Thumbs.db", "page.sync-conflict-20260901.md", "page.md~"]
)
def test_is_debris(name):
    """Verify sync and OS debris is recognized."""
    assert field.is_debris(name) is True


def test_iter_pages_returns_sorted_conformant_files_only(tmp_path):
    """Verify only conformant top-level .md files are pages, and in sorted order."""
    # Given a field with pages, debris, a nonconformant name, and a subdirectory
    (tmp_path / "beta.md").write_text("b")
    (tmp_path / "alpha.md").write_text("a")
    (tmp_path / "Bad Name.md").write_text("x")
    (tmp_path / ".DS_Store").write_text("")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "deep.md").write_text("d")
    (tmp_path / "nomic-embed-text-v1.5.sqlite3").write_bytes(b"")

    # When listing pages
    names = [path.name for path in field.iter_pages(tmp_path)]

    # Then only the two pages appear, sorted
    assert names == ["alpha.md", "beta.md"]


def test_iter_pages_missing_directory_is_empty(tmp_path):
    """Verify a field that does not exist yet lists nothing rather than raising."""
    assert field.iter_pages(tmp_path / "absent") == []


def test_new_page_writes_the_five_fields_and_quoted_datetimes(tmp_path):
    """Verify a new page carries title, uuid, summary, created, updated, with datetimes quoted."""
    # When creating a page
    path = field.new_page(
        tmp_path, title="Carbon Fiber Woks", summary="Thermal notes", body="Prose.", now=NOW
    )

    # Then the file is named from the title and holds the five fields
    assert path.name == "carbon-fiber-woks.md"
    text = path.read_text(encoding="utf-8")
    meta, body = parse(text)
    assert list(meta) == ["title", "uuid", "summary", "created", "updated"]
    assert meta["title"] == "Carbon Fiber Woks"
    uuid.UUID(meta["uuid"])
    assert meta["created"] == NOW
    assert meta["updated"] == NOW
    assert body == "Prose.\n"
    assert f"created: '{NOW}'" in text


def test_new_page_collision_takes_a_numeric_suffix(tmp_path):
    """Verify two pages with one title land in different files inside the flat field."""
    first = field.new_page(tmp_path, title="Same", summary="s", body="", now=NOW)
    second = field.new_page(tmp_path, title="Same", summary="s", body="", now=NOW)

    assert first.name == "same.md"
    assert second.name == "same-2.md"


def test_new_page_refuses_a_title_with_no_slug(tmp_path):
    """Verify a title that slugifies to nothing raises rather than writing '.md'."""
    with pytest.raises(field.PageError):
        field.new_page(tmp_path, title="!!!", summary="s", body="", now=NOW)


def test_claim_filename_retries_a_candidate_lost_to_a_race(tmp_path, mocker):
    """Verify a claim lost to a concurrent writer retries the next candidate.

    `taken` is computed from the directory listing before the loop starts, so it never
    sees a name a competing writer claims after that snapshot; only `atomic.claim`
    returning False models that race.
    """
    real_claim = field.atomic.claim
    calls: list[object] = []

    def flaky_claim(path: object) -> bool:
        calls.append(path)
        return real_claim(path) if len(calls) > 1 else False

    mocker.patch("sessionmemory.lib.field.atomic.claim", side_effect=flaky_claim)

    path = field.claim_filename(tmp_path, "Same")

    assert path.name == "same-2.md"
    assert len(calls) == 2


def test_claim_filename_raises_when_no_name_is_free(tmp_path, mocker):
    """Verify exhausting every candidate raises rather than looping forever."""
    mocker.patch("sessionmemory.lib.field.atomic.claim", return_value=False)

    with pytest.raises(field.PageError, match="no free filename"):
        field.claim_filename(tmp_path, "Same")


def test_create_removes_the_claimed_file_when_writing_content_fails(tmp_path, mocker):
    """Verify a claimed but unwritten file is not left behind after a write failure."""
    mocker.patch("sessionmemory.lib.field.atomic.write_text", side_effect=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        field.new_page(tmp_path, title="T", summary="S", body="B", now=NOW)

    assert list(tmp_path.iterdir()) == []


def test_new_document_writes_title_and_dates_only(tmp_path):
    """Verify a spec or plan carries no uuid and no summary."""
    path = field.new_document(tmp_path, title="A Plan", body="Steps.", now=NOW)

    meta, _ = parse(path.read_text(encoding="utf-8"))
    assert list(meta) == ["title", "created", "updated"]


def test_new_document_honors_an_explicit_stem(tmp_path):
    """Verify a caller can fix the filename, which a dated log needs."""
    path = field.new_document(
        tmp_path, title="Session", body="", now=NOW, stem="2026-09-01-session"
    )
    assert path.name == "2026-09-01-session.md"


def test_read_page_exposes_fields_and_size(tmp_path):
    """Verify a page reads back with its fields and byte size."""
    path = field.new_page(tmp_path, title="T", summary="S", body="B", now=NOW)

    page = field.read_page(path)

    assert page.title == "T"
    assert page.summary == "S"
    assert page.body == "B\n"
    assert page.size == path.stat().st_size
    assert page.created == NOW
    assert page.updated == NOW


def test_read_page_without_frontmatter_is_still_a_page(tmp_path):
    """Verify the spec's rule that pages without frontmatter are valid."""
    path = tmp_path / "bare.md"
    path.write_text("Just prose.\n", encoding="utf-8")

    page = field.read_page(path)

    assert page.meta == {}
    assert page.title == ""
    assert page.body == "Just prose.\n"


def test_write_page_leaves_an_equivalent_file_untouched(tmp_path):
    """Verify a write that changes nothing does not touch the file's mtime."""
    path = field.new_page(tmp_path, title="T", summary="S", body="B", now=NOW)
    page = field.read_page(path)
    before = path.stat().st_mtime_ns

    field.write_page(path, page.meta, page.body)

    assert path.stat().st_mtime_ns == before
