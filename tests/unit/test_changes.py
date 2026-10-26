"""Tests for parsing and rewriting a log's `## Changes` section."""

from __future__ import annotations

from sessionmemory.lib.changes import parse, rewrite

BODY = (
    "## Summary\n\n"
    "Landed aaaaaaa today.\n\n"
    "## Changes\n\n"
    "- aaaaaaa feat: one\n"
    "- bbbbbbb feat: two\n"
    "ccccccc fix: three\n"
    "No other commits.\n\n"
    "## Pages\n\n"
    "- none\n"
)


def test_parse_reads_bare_and_bulleted_lines_in_the_changes_section_only() -> None:
    """Prose outside the section and non-commit lines inside it are ignored."""
    assert [(c.prefix, c.sha, c.subject) for c in parse(BODY)] == [
        ("- ", "aaaaaaa", "feat: one"),
        ("- ", "bbbbbbb", "feat: two"),
        ("", "ccccccc", "fix: three"),
    ]


def test_parse_returns_empty_without_a_changes_heading() -> None:
    """A body with no Changes section has no commit lines."""
    assert parse("## Summary\n\ntext\n") == []


def test_rewrite_replaces_and_collapses_into_one_line() -> None:
    """Several branch commits mapped to one trunk commit become a single line."""
    squashed = "1234567 feat: squashed (#9)"
    result = rewrite(BODY, {"aaaaaaa": squashed, "bbbbbbb": squashed})

    assert result == BODY.replace("- aaaaaaa feat: one\n- bbbbbbb feat: two\n", f"- {squashed}\n")
    assert result.count("aaaaaaa") == 1


def test_rewrite_drops_lines_mapped_to_none() -> None:
    """A None outcome removes only that line."""
    assert rewrite(BODY, {"ccccccc": None}) == BODY.replace("ccccccc fix: three\n", "")


def test_rewrite_skips_a_replacement_already_listed() -> None:
    """A trunk commit already listed is not duplicated."""
    body = "## Changes\n\n- 1234567 feat: squashed (#9)\n- aaaaaaa feat: one\n"
    result = rewrite(body, {"aaaaaaa": "1234567 feat: squashed (#9)"})

    assert result == "## Changes\n\n- 1234567 feat: squashed (#9)\n"


def test_rewrite_without_changes_returns_body_unchanged() -> None:
    """An empty outcome leaves the body as it was."""
    assert rewrite(BODY, {}) == BODY


def test_rewrite_keeps_crlf_endings_and_a_missing_final_newline() -> None:
    """Line endings survive a replacement."""
    body = "## Changes\r\n- aaaaaaa feat: one"
    assert rewrite(body, {"aaaaaaa": "1234567 feat: x"}) == "## Changes\r\n- 1234567 feat: x"


def test_rewrite_skips_a_later_line_already_listed_by_a_replacement() -> None:
    """A kept line repeating a replacement's sha is dropped, keeping the first position."""
    body = "## Changes\n\n- aaaaaaa feat: one\n- 1234567 feat: squashed (#9)\n"
    result = rewrite(body, {"aaaaaaa": "1234567 feat: squashed (#9)"})

    assert result == "## Changes\n\n- 1234567 feat: squashed (#9)\n"


def test_section_heading_is_strict_at_both_ends() -> None:
    """An indented heading neither starts nor ends the section."""
    body = "  ## Changes\n- aaaaaaa feat: one\n## Changes\n- bbbbbbb feat: two\n  ## Other\n- ccccccc fix: x\n## End\n- ddddddd fix: y\n"
    assert [c.sha for c in parse(body)] == ["bbbbbbb", "ccccccc"]


def test_rewrite_leaves_duplicate_unmapped_lines_alone() -> None:
    """Repeated unmapped shas survive, with or without an unrelated outcome."""
    body = "## Changes\n\n- aaaaaaa feat: one\n- aaaaaaa feat: one\n- bbbbbbb feat: two\n"

    assert rewrite(body, {}) == body
    assert rewrite(body, {"bbbbbbb": None}) == body.replace("- bbbbbbb feat: two\n", "")
