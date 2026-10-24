"""Tests for assembling and rendering the session-start block."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sessionmemory.lib import field, inject, registry

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
    field.new_document(root / "specs", title="B Spec", body="", now=NOW, day=NOW[:10])
    field.new_document(root / "plans", title="A Plan", body="", now=NOW, day=NOW[:10])
    (root / "backlog.md").write_text(
        "# Backlog\n\n## feat\n\n- [S] one - 2026-09-01 [#a]\n- [x] [S] done - 2026-09-01\n"
        "- [ ] [S] old open shape - 2026-09-01\n- [M] two - 2026-09-01\n",
        encoding="utf-8",
    )
    return root


def test_build_lists_titles_sorted_and_counts_open_backlog_and_specs(tmp_path):
    """Verify titles come from the pages, sorted, every top-level unticked bullet counts as open, and specs are counted rather than listed."""
    _project(tmp_path)

    result = inject.build(tmp_path, "demo")

    assert result.titles == ("Alpha learning", "Beta learning")
    assert result.open_backlog == 3
    assert result.specs == 2


def test_build_empty_project(tmp_path):
    """Verify a project with nothing yet builds an empty injection rather than failing."""
    result = inject.build(tmp_path, "demo")

    assert result == inject.Injection(
        project="demo", titles=(), open_backlog=0, specs=0, reference=()
    )


def test_render_leads_with_guidance_and_names_the_command(tmp_path):
    """Verify the guidance block comes first and spells the CLI the way the caller asked."""
    _project(tmp_path)

    text = inject.render(inject.build(tmp_path, "demo"), command="/x/bin/sessionmemory")

    assert text.startswith("## Using this vault")
    assert "/x/bin/sessionmemory search" in text
    assert "## What this project knows" in text
    assert "  - Alpha learning\n  - Beta learning" in text
    assert "3 open backlog items" in text
    assert "2 specs in specs/" in text


def test_render_counts_specs_and_never_names_a_spec_or_a_plan(tmp_path):
    """Verify the block carries a spec count and how to find one, and says nothing about plans, which another tool manages."""
    _project(tmp_path)

    text = inject.render(inject.build(tmp_path, "demo"), command="sessionmemory")

    assert "A Spec" not in text
    assert "B Spec" not in text
    assert "`<date>-<topic>.md`" in text
    assert "plan" not in text.casefold()


def test_render_sends_runbooks_to_their_folder(tmp_path):
    """Verify the guidance names the runbooks folder and the command that writes into it."""
    text = inject.render(inject.Injection("demo", (), 0, 0, ()), command="sessionmemory")

    assert "`runbooks/`" in text
    assert 'sessionmemory new runbook --title "..." --cwd .' in text


def test_build_lists_every_reference_doc_by_filename(tmp_path):
    """Verify a doc moved in by hand is listed under its own name, and debris and non-markdown files are not."""
    reference = tmp_path / "projects" / "demo" / "reference"
    field.new_living_document(reference, title="Hosting", body="", now=NOW)
    (reference / "Data Dictionary.md").write_text("# Data\n", encoding="utf-8")
    (reference / "diagram.png").write_bytes(b"")
    (reference / ".DS_Store").write_bytes(b"")
    (reference / "._hosting.md").write_bytes(b"")
    (reference / "Runbook.MD").write_text("# Ops\n", encoding="utf-8")
    (reference / "nested").mkdir()

    result = inject.build(tmp_path, "demo")

    assert result.reference == ("Data Dictionary.md", "Runbook.MD", "hosting.md")


def test_render_lists_reference_docs_under_their_folder(tmp_path):
    """Verify each reference doc is named with its folder so a session can read it directly."""
    text = inject.render(
        inject.Injection("demo", (), 0, 0, ("hosting.md", "operations.md")),
        command="sessionmemory",
    )

    assert "## Reference docs\n\n  reference/hosting.md\n  reference/operations.md" in text
    assert text.index("## Reference docs") < text.index("## Open work")


def test_render_omits_the_reference_section_when_there_are_no_docs(tmp_path):
    """Verify an empty reference folder adds no heading, while the guidance still teaches the folder."""
    text = inject.render(inject.Injection("demo", (), 0, 0, ()), command="sessionmemory")

    assert "## Reference docs" not in text
    assert "`reference/`" in text
    assert 'sessionmemory new reference --title "..." --cwd .' in text


def test_render_singular_spec(tmp_path):
    """Verify one spec reads as a spec, not as 1 specs."""
    text = inject.render(inject.Injection("demo", (), 0, 1, ()), command="sessionmemory")

    assert "1 spec in specs/" in text


def test_guidance_teaches_the_backlog_line_and_the_project_paths():
    """Verify the guidance carries what no skill can be counted on to load: the backlog shape, the paths, and the vault rules."""
    text = inject.GUIDANCE.format(command="sessionmemory")
    # Phrases are matched with whitespace collapsed so a reflow of the text cannot break them.
    flat = " ".join(text.split())

    assert "`- [S] <imperative description> - <YYYY-MM-DD> [#topic]`" in flat
    assert "- [ ]" not in flat
    assert "Delete a finished line" in flat
    assert "`## <kind>` heading" in flat
    assert "sessionmemory new backlog --kind <kind> --size <S|M|L>" in flat
    assert "# Backlog" not in flat
    assert "sessionmemory path <kind>" in flat
    assert "project --json" in flat
    assert "--logs" in flat
    assert "--read" in flat
    assert "remember" in flat
    assert "new learning" in flat
    assert "## decide" in flat
    assert "feat, fix, refactor, perf, docs, test, build, ci, decide" in flat
    assert "top-level bullet" in flat
    assert "must carry a date" not in flat
    bullets = text.split("\n  - ")
    assert any("commit" in bullet and "vault" in bullet for bullet in bullets)


def _learnings(vault: Path, count: int) -> None:
    for number in range(count):
        field.new_page(
            vault / "projects" / "demo" / "learnings",
            title=f"Learning {number:02d}",
            summary="s",
            body="",
            now=NOW,
        )


def test_build_lists_every_runbook_by_filename(tmp_path):
    """Verify runbooks are listed by filename under the same rule as reference docs."""
    runbooks = tmp_path / "projects" / "demo" / "runbooks"
    field.new_living_document(runbooks, title="Deploy", body="", now=NOW)
    (runbooks / "Rotate A Token.md").write_text("# Rotate\n", encoding="utf-8")
    (runbooks / "diagram.png").write_bytes(b"")
    (runbooks / ".DS_Store").write_bytes(b"")
    (runbooks / "._deploy.md").write_bytes(b"")

    result = inject.build(tmp_path, "demo")

    assert result.runbooks == ("Rotate A Token.md", "deploy.md")


def test_render_lists_runbooks_under_their_folder():
    """Verify runbooks render under their own heading, and the heading is absent when there are none."""
    text = inject.render(
        inject.Injection("demo", (), 0, 0, (), runbooks=("rotate-a-token.md",)),
        command="sessionmemory",
    )

    assert "## Runbooks\n\n  runbooks/rotate-a-token.md" in text
    assert "## Runbooks" not in inject.render(
        inject.Injection("demo", (), 0, 0, ()), command="sessionmemory"
    )


def test_review_is_due_with_ten_learnings_never_reviewed(tmp_path):
    """Verify ten learnings and no review date makes a review due and names the slash command."""
    _learnings(tmp_path, 10)

    result = inject.build(tmp_path, "demo", today="2026-10-13")
    text = inject.render(result, command="sessionmemory")

    assert result.review_due is True
    assert result.reviewed == ""
    assert "never" in text
    assert "/sessionmemory:review" in text


def test_review_is_not_due_below_ten_learnings(tmp_path):
    """Verify nine learnings never prompt a review."""
    _learnings(tmp_path, 9)

    result = inject.build(tmp_path, "demo", today="2026-10-13")

    assert result.review_due is False
    assert "/sessionmemory:review" not in inject.render(result, command="sessionmemory")


@pytest.mark.parametrize(("reviewed", "due"), [("2026-09-12", True), ("2026-09-13", False)])
def test_review_is_due_after_thirty_days(tmp_path, reviewed, due):
    """Verify a review is due only when more than thirty days have passed since the last."""
    _learnings(tmp_path, 10)
    registry.save(
        tmp_path,
        {"demo": registry.Project(slug="demo", root="/r", remotes=(), reviewed=reviewed)},
    )

    result = inject.build(tmp_path, "demo", today="2026-10-13")

    assert result.reviewed == reviewed
    assert result.review_due is due


def test_review_with_an_unparsable_date_counts_as_never(tmp_path):
    """Verify a malformed reviewed date is treated as never reviewed rather than raising."""
    _learnings(tmp_path, 10)
    registry.save(
        tmp_path,
        {"demo": registry.Project(slug="demo", root="/r", remotes=(), reviewed="soon")},
    )

    assert inject.build(tmp_path, "demo", today="2026-10-13").review_due is True


@pytest.mark.parametrize("content", [b"[demo\nbroken", b"\xff\xfe\x00bad"])
def test_review_survives_a_registry_that_cannot_be_read(tmp_path, content):
    """Verify an invalid or undecodable registry reads as never reviewed rather than raising."""
    _learnings(tmp_path, 10)
    registry_file = tmp_path / "_system" / "registry.toml"
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    registry_file.write_bytes(content)

    result = inject.build(tmp_path, "demo", today="2026-10-13")

    assert result.reviewed == ""
    assert result.review_due is True


def test_render_puts_the_review_line_after_the_titles(tmp_path):
    """Verify the nudge follows the last title and precedes the open work."""
    _learnings(tmp_path, 10)

    text = inject.render(
        inject.build(tmp_path, "demo", today="2026-10-13"), command="sessionmemory"
    )

    assert text.index("/sessionmemory:review") > text.index("Learning 09")
    assert text.index("/sessionmemory:review") < text.index("## Open work")


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


def test_guidance_teaches_spec_search_summary_and_status():
    """Verify the guidance teaches spec search, the opening paragraph, the statuses, and supersession."""
    flat = " ".join(inject.GUIDANCE.format(command="sessionmemory").split())

    assert 'sessionmemory search "<words>" --specs' in flat
    assert "directly under the" in flat
    assert (
        "A search shows the frontmatter `summary`, else a goal or purpose section's first"
        " paragraph, else that opening one." in flat
    )
    assert "a spec with no frontmatter block gets one holding only `status`" in flat
    for status in ("draft", "shipped", "superseded", "abandoned"):
        assert status in flat
    assert 'new spec --title "..." --supersedes <old filename>' in flat


def _git_repo(path: Path) -> Path:
    import subprocess

    path.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True, capture_output=True)
    return path


def test_build_flags_unadopted_local(tmp_path):
    """Verify a regular CLAUDE.local.md with no vault copy is flagged and the hint names local adopt."""
    vault = tmp_path / "vault"
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.local.md").write_text("# mine\n", encoding="utf-8")

    result = inject.build(vault, "demo", cwd=repo)

    assert result.unadopted_local is True
    text = inject.render(result, command="sessionmemory")
    assert "`sessionmemory local adopt --cwd .`" in text
    assert text.endswith("moves it there")


def test_build_ignores_linked_local(tmp_path):
    """Verify a CLAUDE.local.md already linked into the vault is not flagged."""
    from sessionmemory.lib import localmd

    vault = tmp_path / "vault"
    repo = _git_repo(tmp_path / "repo")
    co = localmd.checkout(vault, "demo", repo)
    co.target.parent.mkdir(parents=True)
    co.target.write_text("# mine\n", encoding="utf-8")
    localmd.link(co)

    assert inject.build(vault, "demo", cwd=repo).unadopted_local is False


def test_build_without_cwd_never_flags(tmp_path):
    """Verify no cwd means no checkout is inspected."""
    assert inject.build(tmp_path, "demo").unadopted_local is False


def test_build_outside_a_work_tree_never_flags(tmp_path):
    """Verify a directory outside git yields no hint rather than an error."""
    plain = tmp_path / "plain"
    plain.mkdir()

    assert inject.build(tmp_path, "demo", cwd=plain).unadopted_local is False


def test_build_ignores_a_tracked_local(tmp_path):
    """Verify a CLAUDE.local.md git tracks is not pointed at adopt, which would refuse it."""
    import subprocess

    vault = tmp_path / "vault"
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.local.md").write_text("# committed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-f", "CLAUDE.local.md"], cwd=repo, check=True, capture_output=True
    )

    assert inject.build(vault, "demo", cwd=repo).unadopted_local is False


def test_build_ignores_a_diverged_local(tmp_path):
    """Verify a regular file beside an existing vault copy is not flagged as unadopted."""
    vault = tmp_path / "vault"
    repo = _git_repo(tmp_path / "repo")
    (repo / "CLAUDE.local.md").write_text("# mine\n", encoding="utf-8")
    copy = vault / "projects" / "demo" / "CLAUDE.local.md"
    copy.parent.mkdir(parents=True)
    copy.write_text("# vault\n", encoding="utf-8")

    assert inject.build(vault, "demo", cwd=repo).unadopted_local is False
