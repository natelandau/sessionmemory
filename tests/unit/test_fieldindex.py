"""Tests for one field's vector index."""

from __future__ import annotations

import dataclasses
import itertools
import json
import sqlite3
import uuid
from typing import TYPE_CHECKING

import pytest

from sessionmemory.lib import field, fieldindex, specs
from sessionmemory.lib.embed import StubEmbedder

if TYPE_CHECKING:
    from pathlib import Path

NOW = "2026-09-01T14:03:11Z"

# Admits every page, for tests about what a search returns rather than which pages are hits.
_ADMIT_ALL = fieldindex.Cutoffs(max_distance=2.0, min_margin=0.0, min_drop=1.0, drop_window=5)


@pytest.fixture(autouse=True)
def _fixed_uuids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin page uuids, since the stub embeds the whole file and a random uuid makes every distance random."""
    counter = itertools.count()
    monkeypatch.setattr(field.uuid, "uuid4", lambda: uuid.UUID(int=next(counter)))


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
    hits = fieldindex.search(tmp_path, StubEmbedder(), matching_text, limit=5, cutoffs=_ADMIT_ALL)

    # Then it is first, with its display fields, and the distance is bounded
    assert [hit.path.name for hit in hits] == ["match.md", "other.md"]
    assert hits[0].title == "Match"
    assert hits[0].summary == "the match"
    assert 0.0 <= hits[0].distance <= 2.0


def test_search_refreshes_before_querying(tmp_path):
    """Verify a page written moments ago is found without an explicit reindex."""
    _page(tmp_path, "Fresh")

    hits = fieldindex.search(tmp_path, StubEmbedder(), "anything", limit=5, cutoffs=_ADMIT_ALL)

    assert [hit.path.name for hit in hits] == ["fresh.md"]


def test_search_missing_field_is_empty(tmp_path):
    """Verify searching a project with no field yet returns nothing and creates nothing."""
    assert fieldindex.search(tmp_path / "absent", StubEmbedder(), "q", limit=5) == []
    assert not (tmp_path / "absent").exists()


def test_search_honors_limit(tmp_path):
    """Verify no more than limit hits come back."""
    for name in ("a", "b", "c"):
        _page(tmp_path, name)

    assert len(fieldindex.search(tmp_path, StubEmbedder(), "q", limit=2, cutoffs=_ADMIT_ALL)) == 2


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
    hits = fieldindex.search(tmp_path, StubEmbedder(), "broken", limit=5, cutoffs=_ADMIT_ALL)
    assert [hit.path.name for hit in hits] == ["invalid.md"]


def test_a_corrupt_index_is_rebuilt(tmp_path):
    """Verify a file that is not a database is discarded and rebuilt rather than crashing a read."""
    _page(tmp_path, "Alpha")
    fieldindex.index_path(tmp_path, StubEmbedder()).write_bytes(b"not a database")

    hits = fieldindex.search(tmp_path, StubEmbedder(), "q", limit=5, cutoffs=_ADMIT_ALL)

    assert [hit.path.name for hit in hits] == ["alpha.md"]


def test_search_drops_hits_farther_than_max_distance(tmp_path):
    """Verify a page beyond the cutoff is not a hit, however few hits there are."""
    # Given a page the stub embeds identically to the query and one it does not
    _page(tmp_path, "Match", summary="the match", body="")
    _page(tmp_path, "Other", summary="other", body="")
    matching_text = (tmp_path / "match.md").read_text(encoding="utf-8")

    # When searching with a cutoff that only the identical page can clear
    hits = fieldindex.search(
        tmp_path,
        StubEmbedder(),
        matching_text,
        limit=5,
        cutoffs=dataclasses.replace(fieldindex.FIELD_CUTOFFS, max_distance=0.5),
    )

    # Then the far page is absent rather than padding the result
    assert [hit.path.name for hit in hits] == ["match.md"]


def test_search_default_cutoff_is_the_measured_one(tmp_path):
    """Verify a search with no cutoff named applies the measured default."""
    _page(tmp_path, "Far")

    assert fieldindex.search(tmp_path, StubEmbedder(), "unrelated", limit=5) == []


def _spec(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_refresh_embeds_a_spec_by_its_derived_input(tmp_path, monkeypatch):
    """Verify a specs directory embeds specs.embedding_input, not the whole file."""
    directory = tmp_path / "specs"
    _spec(directory, "2026-10-03-pricing.md", "# Pricing\n\n## Problem\n\nSECRET-BODY\n")
    seen: list[str] = []
    embedder = StubEmbedder()
    original = embedder.encode_documents
    monkeypatch.setattr(
        embedder, "encode_documents", lambda texts: seen.extend(texts) or original(texts)
    )
    fieldindex.refresh(directory, embedder)
    assert seen == ["Pricing\n\nProblem"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("specs", fieldindex.SPECS_CUTOFFS),
        ("learnings", fieldindex.FIELD_CUTOFFS),
        ("logs", fieldindex.FIELD_CUTOFFS),
    ],
)
def test_default_cutoffs_follow_the_directory(tmp_path, name, expected):
    """Verify specs take their own measured cutoffs and the fields share theirs."""
    assert fieldindex.default_cutoffs(tmp_path / name) == expected


def test_search_in_specs_shows_the_derived_summary_and_status(tmp_path):
    """Verify a spec hit carries the hit summary and its status."""
    directory = tmp_path / "specs"
    _spec(directory, "a.md", "---\ntitle: A\nstatus: shipped\n---\n# A\n\n## Goal\n\nThe goal.\n")
    hits = fieldindex.search(directory, StubEmbedder(), "A", limit=5, cutoffs=_ADMIT_ALL)
    assert (hits[0].summary, hits[0].status) == ("The goal.", "shipped")


def test_search_exclude_does_not_use_up_the_limit(tmp_path):
    """Verify a retired nearest spec is skipped and the next current one still returns."""
    directory = tmp_path / "specs"
    for name, status in (("a.md", "superseded"), ("b.md", "draft"), ("c.md", "abandoned")):
        _spec(directory, name, f"---\ntitle: {name}\nstatus: {status}\n---\n# {name}\n")
    hits = fieldindex.search(
        directory,
        StubEmbedder(),
        "x",
        limit=1,
        cutoffs=_ADMIT_ALL,
        exclude=specs.is_retired,
    )
    assert [h.path.name for h in hits] == ["b.md"]


def test_refresh_indexes_a_spec_with_unparsable_frontmatter(tmp_path):
    """Verify a broken frontmatter block does not keep a spec out of the index."""
    directory = tmp_path / "specs"
    _spec(directory, "broken.md", "---\ntitle: [oops\n---\n# Broken\n")
    assert fieldindex.refresh(directory, StubEmbedder()).added == 1


def test_learning_hits_keep_their_frontmatter_summary(tmp_path):
    """Verify fields outside specs are unchanged: summary from frontmatter, status empty."""
    directory = tmp_path / "learnings"
    directory.mkdir()
    _page(directory, "T", summary="from meta")
    hits = fieldindex.search(directory, StubEmbedder(), "T", limit=1, cutoffs=_ADMIT_ALL)
    assert (hits[0].summary, hits[0].status) == ("from meta", "")


def test_search_in_specs_titles_a_hit_from_its_h1(tmp_path):
    """Verify a spec without a frontmatter title shows its first heading."""
    directory = tmp_path / "specs"
    _spec(directory, "a.md", "# The Heading\n\n## Goal\n\nText.\n")
    hits = fieldindex.search(directory, StubEmbedder(), "x", limit=5, cutoffs=_ADMIT_ALL)
    assert hits[0].title == "The Heading"


def test_search_skips_a_spec_whose_file_cannot_be_read(tmp_path):
    """Verify a spec deleted between the query and its hit read is skipped and the other hit returns."""
    directory = tmp_path / "specs"
    gone = _spec(directory, "a.md", "# A\n")
    _spec(directory, "b.md", "# B\n")

    def delete_a(_meta: dict) -> bool:
        # `exclude` runs for each row before its file is read, after the refresh.
        gone.unlink(missing_ok=True)
        return False

    hits = fieldindex.search(
        directory, StubEmbedder(), "x", limit=5, cutoffs=_ADMIT_ALL, exclude=delete_a
    )
    assert [h.path.name for h in hits] == ["b.md"]


def _user_version(index: Path) -> int:
    conn = sqlite3.connect(index)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _set_user_version(index: Path, version: int) -> None:
    conn = sqlite3.connect(index)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def test_refresh_stamps_a_specs_index_with_the_input_version(tmp_path):
    """Verify a new specs index records the version of the input rule that filled it."""
    directory = tmp_path / "specs"
    _spec(directory, "a.md", "# A\n")
    embedder = StubEmbedder()

    fieldindex.refresh(directory, embedder)

    assert _user_version(fieldindex.index_path(directory, embedder)) == specs.INPUT_VERSION


def test_refresh_reembeds_every_spec_when_the_input_version_differs(tmp_path, monkeypatch):
    """Verify an index stamped with another input version is cleared and every spec re-embedded."""
    directory = tmp_path / "specs"
    _spec(directory, "a.md", "# A\n")
    _spec(directory, "b.md", "# B\n")
    embedder = StubEmbedder()
    fieldindex.refresh(directory, embedder)
    index = fieldindex.index_path(directory, embedder)
    _set_user_version(index, specs.INPUT_VERSION + 1)

    result = fieldindex.refresh(directory, embedder)

    assert (result.added, result.unchanged) == (2, 0)
    assert _user_version(index) == specs.INPUT_VERSION
    assert fieldindex.refresh(directory, embedder).unchanged == 2


def test_refresh_leaves_a_field_index_version_alone(tmp_path):
    """Verify a field's index is neither stamped nor cleared by the specs input version."""
    directory = tmp_path / "learnings"
    directory.mkdir()
    _page(directory, "T")
    embedder = StubEmbedder()
    fieldindex.refresh(directory, embedder)
    index = fieldindex.index_path(directory, embedder)

    result = fieldindex.refresh(directory, embedder)

    assert (result.added, result.unchanged) == (0, 1)
    assert _user_version(index) == 0


def _ranking(*distances: float) -> list[fieldindex.Ranked]:
    return [
        fieldindex.Ranked(filename=f"p{index}.md", frontmatter="{}", distance=distance)
        for index, distance in enumerate(distances)
    ]


_RULE = fieldindex.Cutoffs(max_distance=1.0, min_margin=0.17, min_drop=0.05, drop_window=5)
_MARGIN_ONLY = dataclasses.replace(_RULE, min_drop=1.0)
_DENSE = (0.30, 0.37, 0.375, 0.38, 0.385, 0.39, 0.395, 0.40)


@pytest.mark.parametrize(
    ("distances", "cutoffs", "expected"),
    [
        pytest.param(
            (0.20, 0.60, 0.61, 0.62, 0.63, 0.64, 0.65, 0.66, 0.67),
            _MARGIN_ONLY,
            ["p0.md"],
            id="margin-admits-a-page-far-below-the-median",
        ),
        pytest.param(
            _DENSE, _RULE, ["p0.md"], id="drop-admits-the-answer-in-a-dense-field-the-margin-misses"
        ),
        pytest.param(
            (0.30, 0.31, 0.40, 0.41, 0.42, 0.43, 0.44, 0.45),
            _RULE,
            ["p0.md", "p1.md"],
            id="drop-keeps-a-cluster-above-the-break",
        ),
        pytest.param(
            _DENSE,
            dataclasses.replace(_RULE, max_distance=0.25),
            [],
            id="max-distance-caps-the-drop",
        ),
        pytest.param(
            (0.30, 0.34, 0.375, 0.38, 0.385, 0.39, 0.395, 0.40),
            _RULE,
            [],
            id="a-drop-under-the-minimum-admits-nothing",
        ),
        pytest.param(
            (0.30, 0.31, 0.32, 0.33, 0.34, 0.50, 0.51, 0.52),
            _RULE,
            [],
            id="a-drop-past-the-window-admits-nothing",
        ),
        pytest.param(
            (0.20, 0.60),
            _MARGIN_ONLY,
            ["p0.md"],
            id="small-field-measures-the-margin-against-the-fallback",
        ),
        pytest.param(
            (0.90,),
            dataclasses.replace(_RULE, min_drop=0.0),
            [],
            id="a-single-far-page-has-no-drop",
        ),
        pytest.param(
            (0.50, 0.60, 0.70),
            dataclasses.replace(_MARGIN_ONLY, max_distance=0.65, min_margin=0.0),
            ["p0.md", "p1.md"],
            id="margin-zero-leaves-only-max-distance",
        ),
    ],
)
def test_apply_cutoffs_admits_pages_that_stand_out_by_margin_or_drop(distances, cutoffs, expected):
    """Verify which ranked pages count as hits under each part of the rule."""
    hits = fieldindex.apply_cutoffs(_ranking(*distances), cutoffs, limit=10)
    assert [page.filename for page in hits] == expected
