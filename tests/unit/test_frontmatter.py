"""Tests for YAML frontmatter parsing and serialization."""

from __future__ import annotations

import pytest
import yaml

from sessionmemory.lib.frontmatter import (
    FrontmatterError,
    parse,
    serialize,
    unquoted_datetime_keys,
)

NOTE = """---
type: learning
title: a title
domains:
  - typer
  - pytest
created: 2026-08-01
---

The body.
"""


def test_parse_splits_meta_and_body():
    """Separate the YAML block from the markdown that follows it."""
    meta, body = parse(NOTE)
    assert meta["type"] == "learning"
    assert meta["domains"] == ["typer", "pytest"]
    assert body.strip() == "The body."


def test_parse_returns_dates_as_strings():
    """Keep dates as ISO strings so a round trip does not change their type."""
    meta, _ = parse(NOTE)
    assert meta["created"] == "2026-08-01"
    assert isinstance(meta["created"], str)


def test_parse_missing_frontmatter_raises():
    """Refuse a file with no frontmatter rather than inventing an empty note."""
    with pytest.raises(FrontmatterError, match="no frontmatter"):
        parse("just a body\n")


def test_parse_unterminated_frontmatter_raises():
    """Refuse a file whose frontmatter block never closes."""
    with pytest.raises(FrontmatterError, match="never closed"):
        parse("---\ntype: learning\n\nbody\n")


def test_parse_non_mapping_raises():
    """Refuse frontmatter that is a list or scalar instead of a mapping."""
    with pytest.raises(FrontmatterError, match="mapping"):
        parse("---\n- one\n- two\n---\n\nbody\n")


@pytest.mark.parametrize("raw_meta", ["[]", "false", "0", '""'])
def test_parse_falsy_non_mapping_raises(raw_meta: str):
    """Refuse falsy non-mapping frontmatter instead of treating it as empty.

    ``yaml.safe_load`` returns falsy values like ``[]``, ``False``, ``0``, and
    ``""`` as-is, and only an actually empty block should be normalized to an
    empty mapping.
    """
    with pytest.raises(FrontmatterError, match="mapping"):
        parse(f"---\n{raw_meta}\n---\n\nbody\n")


def test_parse_unclosed_bracket_raises_frontmatter_error():
    """Verify a scalar-level YAML syntax error is wrapped as FrontmatterError, not raised raw.

    A hand-edited note is not guaranteed to be syntactically valid YAML at all, and a
    raw yaml.YAMLError propagating past this module would surprise every caller, since
    parse's own docstring promises FrontmatterError for a malformed block.
    """
    with pytest.raises(FrontmatterError, match="not valid YAML") as excinfo:
        parse("---\nbad: [unclosed\n---\n\nbody\n")
    assert isinstance(excinfo.value.__cause__, yaml.YAMLError)


def test_parse_tab_indentation_raises_frontmatter_error():
    """Verify a tab character in the frontmatter block is wrapped as FrontmatterError."""
    with pytest.raises(FrontmatterError, match="not valid YAML") as excinfo:
        parse("---\nfoo:\n\tbar: baz\n---\n\nbody\n")
    assert isinstance(excinfo.value.__cause__, yaml.YAMLError)


def test_parse_frontmatter_closed_at_eof():
    """Handle a closing delimiter with no trailing newline or body."""
    meta, body = parse("---\ntype: learning\n---")
    assert meta == {"type": "learning"}
    assert body == ""


def test_parse_empty_frontmatter_block():
    """Treat a closing delimiter immediately after the opening one as empty metadata."""
    meta, body = parse("---\n---\n\nbody\n")
    assert meta == {}
    assert body == "\nbody\n"


def test_parse_empty_frontmatter_block_at_eof():
    """Treat an empty block closed at end of file as empty metadata with no body."""
    meta, body = parse("---\n---")
    assert meta == {}
    assert body == ""


def test_parse_tolerates_a_byte_order_mark():
    """Read a note an editor saved with a BOM rather than calling it frontmatter-less."""
    meta, body = parse(f"\ufeff{NOTE}")
    assert meta["type"] == "learning"
    assert body.strip() == "The body."


def test_parse_tolerates_crlf_line_endings():
    """Read a note that arrived from an editor using Windows line endings."""
    meta, body = parse(NOTE.replace("\n", "\r\n"))
    assert meta["domains"] == ["typer", "pytest"]
    assert body.strip() == "The body."
    assert "\r" not in body


def test_round_trip_is_stable():
    """Parsing, serializing, and parsing again yields the same metadata."""
    meta, body = parse(NOTE)
    once = serialize(meta, body)
    meta_again, body_again = parse(once)
    assert meta_again == meta
    assert body_again == body
    assert serialize(meta_again, body_again) == once


def test_serialize_uses_block_lists():
    """Write lists in block style so Obsidian and git diffs stay readable."""
    out = serialize({"domains": ["typer", "pytest"]}, "body")
    assert "- typer" in out
    assert "[typer" not in out


def test_serialize_preserves_key_order():
    """Write keys in the order given rather than alphabetically."""
    out = serialize({"type": "learning", "title": "a", "status": "draft"}, "b")
    assert out.index("type:") < out.index("title:") < out.index("status:")


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        pytest.param(
            "created: 2026-08-14T21:09:44Z\nupdated: 2026-08-14", ["created", "updated"], id="bare"
        ),
        pytest.param("created: '2026-08-14T21:09:44Z'\nupdated: \"2026-08-14\"", [], id="quoted"),
        pytest.param("title: t\nwhen: 2026-08-14", ["when"], id="any-key"),
    ],
)
def test_unquoted_datetime_keys_names_the_values_yaml_types_as_dates(block, expected):
    """Verify a bare date or datetime is reported by key, and a quoted one is not."""
    assert unquoted_datetime_keys(f"---\n{block}\n---\nbody\n") == expected


def test_unquoted_datetime_keys_raises_on_a_block_it_cannot_parse():
    """Verify a malformed block raises the same error parse does, rather than reporting keys."""
    with pytest.raises(FrontmatterError):
        unquoted_datetime_keys("---\ntitle: [\n---\n")
