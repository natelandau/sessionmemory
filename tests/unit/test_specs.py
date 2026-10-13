"""Tests for parsing a spec into title, preamble, and sections."""

from __future__ import annotations

import pytest

from sessionmemory.lib import frontmatter, specs


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Goals", "goals"),
        ("The decisions", "decisions"),
        ("1. Goals", "goals"),
        ("2) Intent", "intent"),
        ("Part A: Decisions", "decisions"),
        ("Decisions (locked)", "decisions"),
        ("  Our Purpose: ", "purpose"),
    ],
)
def test_normalize_heading_strips_numbering_articles_and_parentheticals(heading, expected):
    """Verify every documented normalization step."""
    assert specs.normalize_heading(heading) == expected


@pytest.mark.parametrize(
    ("heading", "matches"),
    [
        ("The decisions", True),
        ("1. Goals", True),
        ("Decision: start fresh, do not adopt the archive", True),
        ("Goals and non-goals", True),
        ("Non-goals", False),
        ("Context and decision", False),
        ("Problem", False),
        ("Today's behavior", False),
    ],
)
def test_heading_matches_only_the_listed_names(heading, matches):
    """Verify the include-list rule from the spec's examples."""
    names = specs.PURPOSE_HEADINGS | specs.DECISION_HEADINGS
    assert specs.heading_matches(heading, names) is matches


@pytest.mark.parametrize(
    ("line", "metadata"),
    [
        ("Date: 2026-06-18 Status: Approved", True),
        ("**Branch:** `feat/diun`", True),
        ("**Date:** 2026-06-14 **Status:** Approved", True),
        ("Today only YouTube links play inside the song page.", False),
        ("A presentation-only redesign of the iPhone app, so it reads well: native.", False),
    ],
)
def test_is_metadata_line(line, metadata):
    """Verify a short capitalized label and colon marks metadata, and prose does not."""
    assert specs.is_metadata_line(line) is metadata


def test_parse_spec_splits_title_preamble_and_sections():
    """Verify title, preamble without metadata, and section bodies including ### subsections."""
    text = (
        "---\ntitle: Data export\n---\n# Data export\n\n**Date:** 2026-10-02\n"
        "A user can take their data out.\n\n## Goal\n\nKeep it.\n\n### Detail\n\nMore.\n\n"
        "## Problem\n\nIt is locked in.\n"
    )
    parsed = specs.parse_spec(text, stem="2026-10-02-data-export")
    assert parsed.title == "Data export"
    assert parsed.preamble == "A user can take their data out."
    assert [s.heading for s in parsed.sections] == ["Goal", "Problem"]
    assert "### Detail" in parsed.sections[0].body
    assert "More." in parsed.sections[0].body


def test_parse_spec_ignores_headings_inside_code_fences():
    """Verify a quoted `## ` line in a fence does not open a section."""
    text = "# T\n\n## Decisions\n\n```markdown\n## Not a section\n```\n\nKept.\n"
    parsed = specs.parse_spec(text, stem="t")
    assert [s.heading for s in parsed.sections] == ["Decisions"]
    assert "Kept." in parsed.sections[0].body


def test_parse_spec_treats_crlf_like_lf():
    """Verify CRLF endings yield the same sections."""
    lf = "# T\n\nIntro.\n\n## Goal\n\nG.\n"
    assert specs.parse_spec(lf.replace("\n", "\r\n"), stem="t") == specs.parse_spec(lf, stem="t")


def test_parse_spec_title_falls_back_to_h1_then_stem():
    """Verify title precedence: frontmatter, then H1, then filename stem."""
    assert specs.parse_spec("# From heading\n", stem="s").title == "From heading"
    assert specs.parse_spec("no heading\n", stem="the-stem").title == "the-stem"


def test_parse_spec_tolerates_unparsable_frontmatter():
    """Verify a broken block is treated as body, not an error."""
    parsed = specs.parse_spec("---\ntitle: [unclosed\n---\n# T\n", stem="t")
    assert parsed.title == "T"


def test_parse_spec_drops_wrapped_metadata_continuation():
    """Verify lines wrapping a metadata entry are dropped with its label."""
    text = "# T\n\n**Scope:** the export path\nand its retry logic.\n\nProse stays.\n\n## G\n"
    assert specs.parse_spec(text, stem="t").preamble == "Prose stays."


@pytest.mark.parametrize("end", [",", ";"])
def test_parse_spec_drops_a_capitalized_line_under_a_metadata_entry_ending_in_a_separator(end):
    """Verify a line wrapping an entry that ends in a comma or semicolon is dropped with it."""
    text = (
        f"# Pricing\n\n**Status:** approved, Stripe Managed Payments{end}\n"
        "Stripe Billing for web, and RevenueCat.\n\nWho pays and for what.\n\n## G\n"
    )
    assert specs.parse_spec(text, stem="t").preamble == "Who pays and for what."


def test_parse_spec_keeps_prose_after_blank_following_metadata():
    """Verify a paragraph after a blank line following a metadata block is kept."""
    text = "# T\n\n**Date:** 2026-10-02\n**Status:** Draft\n\nA user exports data.\n\n## G\n"
    assert specs.parse_spec(text, stem="t").preamble == "A user exports data."


def test_parse_spec_drops_horizontal_rules_from_preamble():
    """Verify rule lines in any of the three styles are dropped."""
    text = "# T\n\n---\n* * *\n___\nIntro.\n\n## G\n"
    assert specs.parse_spec(text, stem="t").preamble == "Intro."


@pytest.mark.parametrize(
    "line", ["- **Date:** 2026-06-21", "* **Status:** Draft", "- Date: 2026-06-21"]
)
def test_is_metadata_line_accepts_bullet_prefix(line):
    """Verify a leading list marker does not hide a metadata label."""
    assert specs.is_metadata_line(line) is True


def test_parse_spec_ignores_headings_inside_tilde_fences():
    """Verify a ~~~ fence hides a `## ` line like a backtick fence."""
    parsed = specs.parse_spec("# T\n\n## A\n\n~~~\n## No\n~~~\n", stem="t")
    assert [s.heading for s in parsed.sections] == ["A"]


def test_parse_spec_unclosed_fence_hides_the_rest():
    """Verify an unclosed fence keeps every later `## ` line inside the section."""
    parsed = specs.parse_spec("# T\n\n## A\n\n```\n## No\n", stem="t")
    assert [s.heading for s in parsed.sections] == ["A"]


def test_parse_spec_without_h1_starts_preamble_at_body():
    """Verify a spec with no H1 takes its preamble from the first body line."""
    parsed = specs.parse_spec("Opening line.\n\n## G\n\nx\n", stem="the-stem")
    assert parsed.preamble == "Opening line."
    assert parsed.title == "the-stem"


def test_parse_spec_frontmatter_title_beats_h1():
    """Verify a frontmatter title wins over a differing H1."""
    parsed = specs.parse_spec("---\ntitle: Front\n---\n# Heading\n", stem="s")
    assert parsed.title == "Front"


SPEC = (
    "---\ntitle: Pricing\nsummary: Who pays and for what.\n---\n# Pricing\n\n"
    "Date: 2026-10-03\nFree tier plus one paid plan.\n\n"
    "## Problem\n\nPROBLEM-TEXT\n\n## Goals\n\nGOAL-TEXT\n\n"
    "## Non-goals\n\nNONGOAL-TEXT\n\n## The decisions\n\nDECISION-TEXT\n"
)


def test_embedding_input_orders_parts_and_drops_unlisted_bodies():
    """Verify part order and that only purpose and decision bodies are embedded."""
    text = specs.embedding_input(SPEC, stem="pricing")
    parts = text.split("\n\n")
    assert parts[0] == "Pricing"
    assert parts[1] == "Free tier plus one paid plan."
    assert parts[2] == "Who pays and for what."
    assert parts[3] == "GOAL-TEXT"
    assert parts[4] == "Problem; Goals; Non-goals; The decisions"
    assert parts[5] == "DECISION-TEXT"
    assert "PROBLEM-TEXT" not in text
    assert "NONGOAL-TEXT" not in text
    assert "2026-10-03" not in text


def test_embedding_input_skips_empty_parts():
    """Verify a spec with only a title and headings yields just those two parts."""
    text = specs.embedding_input("# T\n\n## Problem\n\nx\n\n## Solution\n\ny\n", stem="t")
    assert text == "T\n\nProblem; Solution"


def test_embedding_input_keeps_a_multi_paragraph_preamble_whole():
    """Verify blank lines inside the preamble survive in the joined input."""
    text = specs.embedding_input("# T\n\nFirst para.\n\nSecond para.\n\n## Goal\n\nG\n", stem="t")
    assert text == "T\n\nFirst para.\n\nSecond para.\n\nG\n\nGoal"


def test_cap_at_line_cuts_at_the_last_newline_within_the_limit():
    """Verify the cut keeps whole lines only."""
    text = "a" * 10 + "\n" + "b" * 10 + "\n" + "c" * 10
    assert specs.cap_at_line(text, limit=25) == "a" * 10 + "\n" + "b" * 10


def test_cap_at_line_falls_back_to_a_character_boundary_without_a_newline():
    """Verify one enormous line is still capped, never splitting a character."""
    text = "é" * 10_000
    capped = specs.cap_at_line(text, limit=8192)
    assert len(capped.encode()) <= 8192
    assert capped == "é" * (8192 // 2)


def test_embedding_input_puts_decisions_last_so_a_cut_shortens_them():
    """Verify an oversized spec keeps title, goals, and headings whole."""
    big = "# T\n\n## Goal\n\nGOAL\n\n## Decisions\n\n" + "- decided thing\n" * 1000
    text = specs.embedding_input(big, stem="t")
    assert len(text.encode()) <= 8192
    assert text.startswith("T\n\nGOAL\n\nGoal; Decisions\n\n- decided thing")
    assert text.endswith("- decided thing")


def test_hit_summary_prefers_frontmatter_then_purpose_then_preamble():
    """Verify the summary precedence."""
    assert specs.hit_summary(SPEC) == "Who pays and for what."
    no_meta = SPEC.replace("summary: Who pays and for what.\n", "")
    assert specs.hit_summary(no_meta) == "GOAL-TEXT"
    only_pre = "# T\n\nThe intro paragraph.\n\n## Problem\n\nx\n"
    assert specs.hit_summary(only_pre) == "The intro paragraph."
    assert specs.hit_summary("# T\n\n## Problem\n\n- x\n") == ""


def test_hit_summary_skips_lists_tables_fences_and_quotes():
    """Verify the first prose paragraph, not the first block, is used."""
    text = "# T\n\n## Goal\n\n- a list\n\n| a | b |\n\n> quote\n\n```\ncode\n```\n\nReal prose\nacross lines.\n"
    assert specs.hit_summary(text) == "Real prose across lines."


def test_hit_summary_uses_the_first_preamble_paragraph_of_several():
    """Verify a multi-paragraph preamble yields only its first paragraph."""
    text = "# T\n\nFirst para\nwraps.\n\nSecond para.\n\n## Problem\n\nx\n"
    assert specs.hit_summary(text) == "First para wraps."


def test_hit_summary_cuts_at_a_word_boundary():
    """Verify a long paragraph is cut to at most 300 characters at a space."""
    text = "# T\n\n## Goal\n\n" + "word " * 200 + "\n"
    summary = specs.hit_summary(text)
    assert len(summary) <= 300
    assert not summary.endswith(" ")
    assert summary.split()[-1] == "word"


def test_hit_summary_falls_back_to_any_section_prose():
    """Verify a Problem paragraph is used when purpose and preamble have no prose."""
    text = "# T\n\n## Goals\n\n1. a\n2. b\n\n## Problem\n\nThe real problem.\n"
    assert specs.hit_summary(text) == "The real problem."


def test_hit_summary_fallback_skips_a_status_section():
    """Verify Status prose is never the summary."""
    text = "# T\n\n## Status\n\nDraft in review.\n\n## Why\n\nBecause.\n"
    assert specs.hit_summary(text) == "Because."


@pytest.mark.parametrize("meta", ["summary: ''", "summary: 5", "summary: '  '"])
def test_hit_summary_blank_or_non_string_summary_falls_through(meta):
    """Verify an unusable frontmatter summary defers to the body."""
    text = f"---\n{meta}\n---\n# T\n\nIntro.\n"
    assert specs.hit_summary(text) == "Intro."


def test_hit_summary_heading_directly_after_prose_ends_the_paragraph():
    """Verify a heading line is not joined into the paragraph above it."""
    text = "# T\n\n## Goal\n\nProse here.\n### Sub\nmore\n"
    assert specs.hit_summary(text) == "Prose here."


def test_hit_summary_reads_a_leading_hash_without_a_space_as_prose():
    """Verify `#42 is failing` opens a paragraph rather than counting as a heading."""
    assert specs.hit_summary("# T\n\n## Goal\n\n#42 is failing in CI.\n") == (
        "#42 is failing in CI."
    )
    assert specs.hit_summary("# T\n\n## Goal\n\nThe build broke.\n#42 tracks it.\n") == (
        "The build broke. #42 tracks it."
    )


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"status": "superseded"}, True),
        ({"status": "abandoned"}, True),
        ({"status": "draft"}, False),
        ({"status": "shipped"}, False),
        ({}, False),
        ({"status": 5}, False),
    ],
)
def test_is_retired(meta, expected):
    """Verify only superseded and abandoned specs count as retired."""
    assert specs.is_retired(meta) is expected


@pytest.mark.parametrize("name", ["x", "x.md", "specs/x.md", "/a/b/x.md"])
def test_resolve_name_accepts_stem_filename_and_path(tmp_path, name):
    """Verify a stem, a filename, and a path all resolve to the same file."""
    (tmp_path / "x.md").write_text("body", encoding="utf-8")

    assert specs.resolve_name(tmp_path, name) == tmp_path / "x.md"


def test_resolve_name_rejects_a_missing_spec(tmp_path):
    """Verify a missing spec raises SpecError naming it."""
    with pytest.raises(specs.SpecError, match="no spec named 'x'"):
        specs.resolve_name(tmp_path, "x")


def test_mark_superseded_keeps_other_keys_and_body(tmp_path):
    """Verify the status and link are set and everything else is kept."""
    path = tmp_path / "x.md"
    path.write_text("---\ntitle: X\nowner: me\n---\n# X\n\nBody.\n", encoding="utf-8")

    specs.mark_superseded(path, by="y.md")

    meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    assert meta == {"title": "X", "owner": "me", "status": "superseded", "superseded_by": "y.md"}
    assert body.rstrip() == "# X\n\nBody."


def test_mark_superseded_adds_a_block_to_a_spec_without_one(tmp_path):
    """Verify a spec with no frontmatter gets a block holding only the two keys."""
    path = tmp_path / "x.md"
    path.write_text("# X\n\nBody.\n", encoding="utf-8")

    specs.mark_superseded(path, by="y.md")

    meta, body = frontmatter.parse(path.read_text(encoding="utf-8"))
    assert meta == {"status": "superseded", "superseded_by": "y.md"}
    assert body.rstrip() == "# X\n\nBody."


def test_mark_superseded_refuses_a_broken_block(tmp_path):
    """Verify an unparsable frontmatter block raises SpecError and leaves the file alone."""
    path = tmp_path / "x.md"
    path.write_text("---\ntitle: [x\n---\nbody\n", encoding="utf-8")

    with pytest.raises(specs.SpecError):
        specs.mark_superseded(path, by="y.md")

    assert path.read_text(encoding="utf-8") == "---\ntitle: [x\n---\nbody\n"


def test_mark_superseded_refuses_invalid_utf8_and_writes_nothing(tmp_path):
    """Verify a spec that is not valid UTF-8 raises SpecError naming it and is left alone."""
    path = tmp_path / "x.md"
    raw = b"---\ntitle: X\n---\n# X\n\nBad \xff byte.\n"
    path.write_bytes(raw)

    with pytest.raises(specs.SpecError, match=r"x\.md"):
        specs.mark_superseded(path, by="y.md")

    assert path.read_bytes() == raw


def test_mark_superseded_drops_a_bom_from_a_spec_without_a_block(tmp_path):
    """Verify a BOM-prefixed spec with no block keeps its H1 directly after the new block."""
    path = tmp_path / "x.md"
    path.write_text("\ufeff# X\n\nBody.\n", encoding="utf-8")

    specs.mark_superseded(path, by="y.md")

    text = path.read_text(encoding="utf-8")
    assert "\ufeff" not in text
    assert text.endswith("---\n# X\n\nBody.\n")


def test_status_problems_reports_an_unknown_status():
    """Verify a status outside the four is named with its repr."""
    assert specs.status_problems({"status": "done"}, filename="a.md", siblings=set()) == [
        "status 'done' is not one of draft, shipped, superseded, abandoned"
    ]


def test_status_problems_reports_a_non_string_status():
    """Verify a non-string status reports the same message with its repr."""
    assert specs.status_problems({"status": 3}, filename="a.md", siblings=set()) == [
        "status 3 is not one of draft, shipped, superseded, abandoned"
    ]


def test_status_problems_ignores_a_missing_status():
    """Verify a spec with no status is current and reports nothing."""
    assert specs.status_problems({}, filename="a.md", siblings=set()) == []


def test_status_problems_reports_superseded_without_a_successor():
    """Verify a superseded spec must say what replaced it."""
    assert specs.status_problems({"status": "superseded"}, filename="a.md", siblings=set()) == [
        "superseded without superseded_by"
    ]


def test_status_problems_reports_a_dangling_successor():
    """Verify superseded_by must name a sibling spec."""
    meta = {"status": "superseded", "superseded_by": "gone"}
    assert specs.status_problems(meta, filename="a.md", siblings={"a.md", "b.md"}) == [
        "superseded_by 'gone' names no spec in specs/"
    ]


@pytest.mark.parametrize("name", ["b", "b.md"])
def test_status_problems_accepts_a_successor_with_or_without_extension(name):
    """Verify superseded_by resolves with or without `.md`."""
    meta = {"status": "superseded", "superseded_by": name}
    assert specs.status_problems(meta, filename="a.md", siblings={"a.md", "b.md"}) == []


@pytest.mark.parametrize("name", ["a", "a.md"])
def test_status_problems_reports_a_spec_superseded_by_itself(name):
    """Verify superseded_by naming the spec's own file is reported, not accepted as a sibling."""
    meta = {"status": "superseded", "superseded_by": name}
    assert specs.status_problems(meta, filename="a.md", siblings={"a.md", "b.md"}) == [
        "superseded_by names this spec"
    ]
