"""Tests for note id generation."""

from __future__ import annotations

import pytest

from sessionmemory.lib.ids import (
    MAX_ID_LENGTH,
    id_candidates,
    slugify,
    strip_date,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("typer vendored click", "typer-vendored-click"),
        ("Typer Vendored Click", "typer-vendored-click"),
        ("uv sync: dev extras!", "uv-sync-dev-extras"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("multiple---dashes", "multiple-dashes"),
        ("café naïve", "cafe-naive"),
        ("python 3.13 upgrade", "python-3-13-upgrade"),
    ],
)
def test_slugify(title, expected):
    """Reduce a title to a lowercase, hyphenated, ascii slug."""
    assert slugify(title) == expected


def test_slugify_truncates_at_a_word_boundary():
    """Keep ids short enough to read, cutting between words."""
    slug = slugify("word " * 40)
    assert len(slug) <= MAX_ID_LENGTH
    assert not slug.endswith("-")


def test_slugify_empty_title_raises():
    """Refuse a title with no slug-able characters rather than emitting an empty id."""
    with pytest.raises(ValueError, match="no usable characters"):
        slugify("!!!")


def test_id_candidates_yields_base_first_when_free():
    """Use the plain slug when nothing has claimed it."""
    assert next(id_candidates("typer-click", set())) == "typer-click"


def test_id_candidates_skips_taken_candidates_appending_a_counter():
    """Skip candidates already claimed, appending a counter starting at 2."""
    candidates = id_candidates("typer-click", {"typer-click", "typer-click-2"})
    assert next(candidates) == "typer-click-3"


def test_id_candidates_keeps_every_candidate_within_the_limit():
    """Trim the base so a suffixed candidate still fits the length limit."""
    base = "a" * MAX_ID_LENGTH
    candidates = id_candidates(base, {base})
    result = next(candidates)
    assert len(result) <= MAX_ID_LENGTH
    assert result.endswith("-2")


@pytest.mark.parametrize(
    ("title", "created", "expected"),
    [
        ("2026-08-18 vault rollout", "2026-08-18", "vault rollout"),
        ("vault rollout 2026-08-18", "2026-08-18", "vault rollout"),
        ("vault rollout on 2026-03-01", "2026-08-18", "vault rollout on 2026-03-01"),
        ("vault rollout", "2026-08-18", "vault rollout"),
    ],
)
def test_strip_date(title, created, expected):
    """Remove only a date the filename is about to supply, leaving any other date alone."""
    assert strip_date(title, created) == expected


def test_strip_date_keeps_a_title_that_is_nothing_but_its_date():
    """Return the title unchanged rather than leaving nothing to slugify."""
    assert strip_date("2026-08-18", "2026-08-18") == "2026-08-18"


def test_slugify_honors_a_caller_supplied_length_budget():
    """Let a dated id reserve room for its prefix without changing the shared limit."""
    slug = slugify("word " * 40, max_length=20)
    assert len(slug) <= 20
    assert not slug.endswith("-")
