"""Tests for assembling and rendering the session-start block."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sessionmemory.lib import field, inject

if TYPE_CHECKING:
    from pathlib import Path

NOW = "2026-09-01T14:03:11Z"


def _project(vault: Path) -> Path:
    root = vault / "projects" / "demo"
    field.new_page(
        root / "learnings",
        title="Beta learning",
        summary="SUMMARY-SENTINEL-beta",
        body="BODY-SENTINEL-beta",
        now=NOW,
    )
    field.new_page(
        root / "learnings",
        title="Alpha learning",
        summary="SUMMARY-SENTINEL-alpha",
        body="BODY-SENTINEL-alpha",
        now=NOW,
    )
    field.new_document(root / "specs", title="A Spec", body="", now=NOW, day=NOW[:10])
    (root / "backlog.md").write_text(
        "# Backlog\n\n## feat\n\n- [S] one - 2026-09-01 [#a]\n- [x] [S] done - 2026-09-01\n"
        "- [ ] [S] old open shape - 2026-09-01\n- [M] two - 2026-09-01\n",
        encoding="utf-8",
    )
    return root


def test_build_lists_titles_sorted_and_counts_open_backlog(tmp_path):
    """Verify titles come from the pages, sorted, and only lines in the item shape count as open."""
    _project(tmp_path)

    result = inject.build(tmp_path, "demo")

    assert result.titles == ("Alpha learning", "Beta learning")
    assert result.open_backlog == 2
    assert result.specs == ("A Spec",)
    assert result.plans == ()


def test_build_empty_project(tmp_path):
    """Verify a project with nothing yet builds an empty injection rather than failing."""
    result = inject.build(tmp_path, "demo")

    assert result == inject.Injection(project="demo", titles=(), open_backlog=0, specs=(), plans=())


def test_render_leads_with_guidance_and_names_the_command(tmp_path):
    """Verify the guidance block comes first and spells the CLI the way the caller asked."""
    _project(tmp_path)

    text = inject.render(inject.build(tmp_path, "demo"), command="/x/bin/sessionmemory")

    assert text.startswith("## Using this vault")
    assert "/x/bin/sessionmemory search" in text
    assert "## What this project knows" in text
    assert "  - Alpha learning\n  - Beta learning" in text
    assert "2 open backlog items" in text
    assert "A Spec" in text


def test_guidance_teaches_the_backlog_line_and_the_project_paths():
    """Verify the guidance carries what no skill can be counted on to load: the backlog item shape and where every file lives."""
    text = inject.GUIDANCE.format(command="sessionmemory")

    assert "`- [S] <imperative description> - <YYYY-MM-DD> [#topic]`" in text
    assert "- [ ]" not in text
    assert "Delete a finished line" in text
    assert "`## <kind>` heading" in text
    assert "sessionmemory new backlog --kind <kind> --size <S|M|L>" in text
    assert "# Backlog" not in text
    assert "sessionmemory project --json" in text
    assert "--logs" in text
    assert "--read" in text


def test_titles_sort_by_their_first_word_not_their_first_character(tmp_path):
    """Verify a title that opens with a code span or a quote sorts with its word rather than ahead of the alphabet."""
    learnings = tmp_path / "projects" / "demo" / "learnings"
    for title in ("Beta", "`alpha.module` skips in check mode", '"Gamma" is quoted', "Delta"):
        field.new_page(learnings, title=title, summary="s", body="", now=NOW)

    result = inject.build(tmp_path, "demo")

    assert result.titles == (
        "`alpha.module` skips in check mode",
        "Beta",
        "Delta",
        '"Gamma" is quoted',
    )


def test_render_names_an_empty_project_rather_than_an_empty_list(tmp_path):
    """Verify a project with no learnings says so instead of rendering nothing."""
    text = inject.render(inject.build(tmp_path, "demo"), command="sessionmemory")

    assert "  nothing yet" in text


def test_render_carries_no_summaries_or_bodies(tmp_path):
    """Verify injection is titles only, carrying neither a page's summary nor its body."""
    _project(tmp_path)

    text = inject.render(inject.build(tmp_path, "demo"), command="sessionmemory")

    assert "Alpha learning" in text
    assert "Beta learning" in text
    assert "SUMMARY-SENTINEL-alpha" not in text
    assert "SUMMARY-SENTINEL-beta" not in text
    assert "BODY-SENTINEL-alpha" not in text
    assert "BODY-SENTINEL-beta" not in text
