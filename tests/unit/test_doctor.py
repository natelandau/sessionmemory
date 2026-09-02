"""Tests for the six doctor checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sessionmemory.lib import doctor, field, fieldindex, registry
from sessionmemory.lib.embed import StubEmbedder

if TYPE_CHECKING:
    from pathlib import Path

NOW = "2026-09-01T14:03:11Z"


def _learnings(vault) -> Path:
    directory = vault / "projects" / "demo" / "learnings"
    directory.mkdir(parents=True)
    return directory


def _logs(vault) -> Path:
    directory = vault / "projects" / "demo" / "logs"
    directory.mkdir(parents=True)
    return directory


def test_nonconformant_names_reports_a_bad_name_only(tmp_path):
    """Verify a capitalized or underscored name is reported and a good one is not."""
    directory = _learnings(tmp_path)
    (directory / "Good Name.md").write_text("x")
    field.new_page(directory, title="fine", summary="s", body="", now=NOW)

    findings = doctor.nonconformant_names(tmp_path, StubEmbedder())

    assert [f.path for f in findings] == [str(directory / "Good Name.md")]


def test_oversized_pages_reports_over_the_limit_only(tmp_path):
    """Verify a page over 8192 bytes is reported and one under is not."""
    directory = _learnings(tmp_path)
    field.new_page(directory, title="big", summary="s", body="x" * 9000, now=NOW)
    field.new_page(directory, title="small", summary="s", body="x", now=NOW)

    findings = doctor.oversized_pages(tmp_path, StubEmbedder())

    assert [f.path for f in findings] == [str(directory / "big.md")]


def test_malformed_frontmatter_reports_an_unclosed_block(tmp_path):
    """Verify unparsable YAML is reported."""
    directory = _learnings(tmp_path)
    (directory / "broken.md").write_text("---\ntitle: [\n---\nx\n", encoding="utf-8")
    field.new_page(directory, title="fine", summary="s", body="", now=NOW)

    findings = doctor.malformed_frontmatter(tmp_path, StubEmbedder())

    assert [f.path for f in findings] == [str(directory / "broken.md")]


def test_malformed_frontmatter_reports_a_bare_page_in_logs_as_fine(tmp_path):
    """Verify a page with no frontmatter is fine in logs, where nothing is required."""
    directory = _logs(tmp_path)
    (directory / "bare.md").write_text("just prose\n", encoding="utf-8")

    findings = doctor.malformed_frontmatter(tmp_path, StubEmbedder())

    assert findings == []


def test_malformed_frontmatter_reports_a_learnings_page_with_no_frontmatter(tmp_path):
    """Verify a learnings page with no frontmatter block reports missing title and summary."""
    directory = _learnings(tmp_path)
    (directory / "bare.md").write_text("just prose\n", encoding="utf-8")

    findings = doctor.malformed_frontmatter(tmp_path, StubEmbedder())

    assert len(findings) == 1
    assert findings[0].message == "missing title or summary"


def test_malformed_frontmatter_reports_a_learnings_page_missing_title_or_summary(tmp_path):
    """Verify a learnings page with an empty title or summary is reported."""
    directory = _learnings(tmp_path)
    (directory / "blank.md").write_text(
        '---\ntitle: ""\nsummary: s\n---\n\nbody\n', encoding="utf-8"
    )

    findings = doctor.malformed_frontmatter(tmp_path, StubEmbedder())

    assert len(findings) == 1
    assert findings[0].message == "missing title or summary"


def test_malformed_frontmatter_stays_quiet_for_a_complete_learnings_page(tmp_path):
    """Verify a learnings page carrying both title and summary is not reported."""
    directory = _learnings(tmp_path)
    field.new_page(directory, title="fine", summary="s", body="", now=NOW)

    findings = doctor.malformed_frontmatter(tmp_path, StubEmbedder())

    assert findings == []


def test_malformed_frontmatter_reports_a_page_with_invalid_utf8(tmp_path):
    """Verify a page saved with invalid UTF-8 is reported rather than crashing doctor."""
    directory = _learnings(tmp_path)
    (directory / "invalid.md").write_bytes(b"---\ntitle: t\nsummary: s\n---\n\xff\xfe\n")

    findings = doctor.malformed_frontmatter(tmp_path, StubEmbedder())

    assert [f.path for f in findings] == [str(directory / "invalid.md")]
    assert findings[0].message == "not valid UTF-8"


def test_malformed_frontmatter_reports_a_bom_prefixed_page(tmp_path):
    """Verify a BOM-prefixed page with malformed frontmatter is reported."""
    directory = _learnings(tmp_path)
    (directory / "bom.md").write_bytes(b"\xef\xbb\xbf---\ntitle: [\n---\nx\n")

    findings = doctor.malformed_frontmatter(tmp_path, StubEmbedder())

    assert [f.path for f in findings] == [str(directory / "bom.md")]


def test_dead_projects_reports_a_registered_root_that_is_gone(tmp_path):
    """Verify a registry entry whose root no longer exists is reported."""
    alive = tmp_path / "alive"
    alive.mkdir()
    registry.save(
        tmp_path,
        {
            "alive": registry.Project(slug="alive", remotes=(), root=str(alive)),
            "gone": registry.Project(slug="gone", remotes=(), root=str(tmp_path / "gone")),
        },
    )

    findings = doctor.dead_projects(tmp_path, StubEmbedder())

    assert [f.path for f in findings] == ["gone"]


def test_dead_projects_reports_a_malformed_registry(tmp_path):
    """Verify a malformed registry file is reported."""
    system_dir = tmp_path / "_system"
    system_dir.mkdir()
    (system_dir / "registry.toml").write_text("not = valid = toml\n")

    findings = doctor.dead_projects(tmp_path, StubEmbedder())

    assert len(findings) == 1
    assert findings[0].check == "registry"


def test_stale_indexes_reports_an_unreadable_index_and_a_stale_one(tmp_path):
    """Verify a non-database file and an index behind its pages are both reported."""
    learnings = _learnings(tmp_path)
    field.new_page(learnings, title="a", summary="s", body="", now=NOW)
    fieldindex.refresh(learnings, StubEmbedder())
    field.new_page(learnings, title="b", summary="s", body="", now=NOW)
    logs = tmp_path / "projects" / "demo" / "logs"
    logs.mkdir()
    fieldindex.index_path(logs, StubEmbedder()).write_bytes(b"junk")

    findings = doctor.stale_indexes(tmp_path, StubEmbedder())

    assert {f.path for f in findings} == {str(learnings), str(logs)}


def test_run_on_a_clean_vault_finds_nothing(tmp_path):
    """Verify a conformant vault with fresh indexes produces no findings."""
    learnings = _learnings(tmp_path)
    field.new_page(learnings, title="a", summary="s", body="", now=NOW)
    fieldindex.refresh(learnings, StubEmbedder())

    assert doctor.run(tmp_path, StubEmbedder()) == []


def test_unquoted_datetimes_reports_a_page_whose_dates_are_bare(tmp_path):
    """Verify a bare datetime is reported by key, since YAML 1.1 parsers type it silently."""
    directory = _learnings(tmp_path)
    (directory / "bare.md").write_text(
        "---\ntitle: t\nsummary: s\ncreated: 2026-08-14T21:09:44Z\nupdated: 2026-08-14\n---\n",
        encoding="utf-8",
    )

    findings = doctor.unquoted_datetimes(tmp_path, StubEmbedder())

    assert findings == [
        doctor.Finding(
            "datetime", str(directory / "bare.md"), "created, updated: unquoted; quote the value"
        )
    ]


def test_unquoted_datetimes_stays_quiet_for_quoted_dates_and_unparsable_blocks(tmp_path):
    """Verify a page this CLI wrote is fine, and a malformed block is left to the frontmatter check."""
    directory = _learnings(tmp_path)
    field.new_page(directory, title="fine", summary="s", body="", now=NOW)
    (directory / "broken.md").write_text("---\ntitle: [\n---\n", encoding="utf-8")
    (directory / "bare.md").write_text("no frontmatter\n", encoding="utf-8")

    assert doctor.unquoted_datetimes(tmp_path, StubEmbedder()) == []


def test_run_includes_the_datetime_check(tmp_path):
    """Verify the check runs as part of doctor rather than only when called by name."""
    assert doctor.unquoted_datetimes in doctor.CHECKS
