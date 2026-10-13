"""Tests for the doctor checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sessionmemory.lib import doctor, field, fieldindex, registry
from sessionmemory.lib.embed import StubEmbedder

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


def test_stale_indexes_reports_a_specs_index_behind_its_specs(tmp_path):
    """Verify the specs directory is checked for a stale index like a field."""
    specs = tmp_path / "projects" / "demo" / "specs"
    specs.mkdir(parents=True)
    spec = specs / "2026-09-01-a.md"
    spec.write_text("# A\n\nGoal text.\n", encoding="utf-8")
    fieldindex.refresh(specs, StubEmbedder())
    spec.write_text("# A\n\nChanged text.\n", encoding="utf-8")

    findings = doctor.stale_indexes(tmp_path, StubEmbedder())

    assert [f.path for f in findings] == [str(specs)]


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


def test_ticked_backlog_lines_reports_only_ticked_lines(tmp_path):
    """Verify the backlog check reports a ticked line and nothing a person wrote by hand."""
    directory = _learnings(tmp_path)
    backlog = directory.parent / "backlog.md"
    backlog.write_text(
        "# Backlog\n\n## feat\n\n- [x] done\n- plain prose\n- [M] undated\n  - note\n",
        encoding="utf-8",
    )

    findings = doctor.ticked_backlog_lines(tmp_path, StubEmbedder())

    assert [f.message for f in findings] == ["line 5: ticked; finished work is deleted, not ticked"]
    assert [(f.check, f.path) for f in findings] == [("backlog", str(backlog))]


def test_ticked_backlog_lines_stays_quiet_without_a_backlog(tmp_path):
    """Verify a project with no backlog.md is not a finding."""
    _learnings(tmp_path)

    assert doctor.ticked_backlog_lines(tmp_path, StubEmbedder()) == []


def test_run_includes_the_backlog_check(tmp_path):
    """Verify a ticked backlog line surfaces through `run`."""
    directory = _learnings(tmp_path)
    (directory.parent / "backlog.md").write_text("- [x] [S] done - 2026-09-01\n", encoding="utf-8")

    findings = doctor.run(tmp_path, StubEmbedder())

    assert [f.check for f in findings] == ["backlog"]


def _project_file(vault, slug, folder, name, text) -> Path:
    directory = vault / "projects" / slug / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_dangling_references_reports_a_missing_runbook(tmp_path):
    """Verify a spec naming a runbook that does not exist yields one finding."""
    spec = _project_file(tmp_path, "demo", "specs", "s.md", "see `runbooks/rotate-a-token.md`\n")

    findings = doctor.dangling_references(tmp_path, StubEmbedder())

    assert [(f.check, f.path) for f in findings] == [("reference", str(spec))]
    assert "line 1" in findings[0].message
    assert "runbooks/rotate-a-token.md" in findings[0].message


def test_dangling_references_resolves_a_named_project(tmp_path):
    """Verify a mention naming another project resolves inside that project."""
    _project_file(tmp_path, "a", "learnings", "l.md", "see projects/b/specs/x.md\n")
    target = _project_file(tmp_path, "b", "specs", "x.md", "x")

    assert doctor.dangling_references(tmp_path, StubEmbedder()) == []

    target.unlink()

    assert len(doctor.dangling_references(tmp_path, StubEmbedder())) == 1


def test_dangling_references_accepts_the_vault_absolute_path(tmp_path):
    """Verify a mention spelled with the vault's absolute path resolves."""
    target = _project_file(tmp_path, "a", "reference", "hosting.md", "x")
    _project_file(tmp_path, "a", "specs", "s.md", f"see {target}\n")

    assert doctor.dangling_references(tmp_path, StubEmbedder()) == []


def test_dangling_references_ignores_plans(tmp_path):
    """Verify a mention of a plan is never checked."""
    _project_file(tmp_path, "a", "specs", "s.md", "see plans/2026-10-01-x.md\n")

    assert doctor.dangling_references(tmp_path, StubEmbedder()) == []


def test_dangling_references_ignores_placeholders_and_repository_paths(tmp_path):
    """Verify placeholders and paths inside a repository are not vault mentions."""
    text = "specs/<date>-<slug>.md\ndocs/specs/x.md\nweb/src/reference/a.md\n"
    _project_file(tmp_path, "a", "specs", "s.md", text)

    assert doctor.dangling_references(tmp_path, StubEmbedder()) == []


def test_dangling_references_does_not_read_logs_or_plans(tmp_path):
    """Verify only specs, runbooks, reference, and learnings are scanned."""
    _project_file(tmp_path, "a", "logs", "l.md", "see specs/missing.md\n")
    _project_file(tmp_path, "a", "plans", "p.md", "see specs/missing.md\n")

    assert doctor.dangling_references(tmp_path, StubEmbedder()) == []


def test_dangling_references_ignores_a_longer_extension(tmp_path):
    """Verify `specs/foo.mdx` is not read as a mention of `specs/foo.md`."""
    _project_file(tmp_path, "a", "specs", "s.md", "see specs/foo.mdx and runbooks/bar.md_old\n")

    assert doctor.dangling_references(tmp_path, StubEmbedder()) == []


def test_dangling_references_allows_a_sentence_ending_period(tmp_path):
    """Verify a mention closing a sentence is still checked."""
    _project_file(tmp_path, "a", "specs", "s.md", "See specs/missing.md.\n")

    findings = doctor.dangling_references(tmp_path, StubEmbedder())

    assert ["specs/missing.md" in f.message for f in findings] == [True]


def test_dangling_references_checks_a_log_target(tmp_path):
    """Verify a mention of a log resolves in `logs/` and is reported when missing."""
    _project_file(tmp_path, "a", "learnings", "l.md", "from logs/2026-10-01-deploy.md\n")

    findings = doctor.dangling_references(tmp_path, StubEmbedder())

    assert ["logs/2026-10-01-deploy.md" in f.message for f in findings] == [True]

    _project_file(tmp_path, "a", "logs", "2026-10-01-deploy.md", "x")

    assert doctor.dangling_references(tmp_path, StubEmbedder()) == []


def test_dangling_references_reports_each_mention_on_one_line(tmp_path):
    """Verify two missing mentions on one line yield two findings."""
    _project_file(tmp_path, "a", "specs", "s.md", "see specs/one.md and runbooks/two.md\n")

    findings = doctor.dangling_references(tmp_path, StubEmbedder())

    assert [f.message for f in findings] == [
        "line 1: names specs/one.md, which does not exist",
        "line 1: names runbooks/two.md, which does not exist",
    ]


def test_spec_statuses_reports_each_problem(tmp_path):
    """Verify each bad spec yields its finding, and a good spec or one with no block yields none."""
    _project_file(tmp_path, "demo", "specs", "new.md", "---\nstatus: draft\n---\n# New\n")
    _project_file(tmp_path, "demo", "specs", "plain.md", "# Plain\n\nNo block at all.\n")
    _project_file(tmp_path, "demo", "specs", "odd.md", "---\nstatus: done\n---\n# Odd\n")
    _project_file(tmp_path, "demo", "specs", "bare.md", "---\nstatus: superseded\n---\n# Bare\n")
    _project_file(
        tmp_path,
        "demo",
        "specs",
        "dangling.md",
        "---\nstatus: superseded\nsuperseded_by: nope.md\n---\n# Dangling\n",
    )
    _project_file(
        tmp_path,
        "demo",
        "specs",
        "itself.md",
        "---\nstatus: superseded\nsuperseded_by: itself\n---\n# Itself\n",
    )
    _project_file(
        tmp_path,
        "demo",
        "specs",
        "ok.md",
        "---\nstatus: superseded\nsuperseded_by: new\n---\n# Ok\n",
    )
    _project_file(tmp_path, "demo", "specs", "broken.md", "---\nstatus: [x\n---\n# Broken\n")

    findings = doctor.spec_statuses(tmp_path, StubEmbedder())

    assert {(Path(f.path).name, f.message) for f in findings} == {
        ("odd.md", "status 'done' is not one of draft, shipped, superseded, abandoned"),
        ("bare.md", "superseded without superseded_by"),
        ("dangling.md", "superseded_by 'nope.md' names no spec in specs/"),
        ("itself.md", "superseded_by names this spec"),
        ("broken.md", "frontmatter cannot be parsed; status ignored"),
    }
    assert {f.check for f in findings} == {"spec"}


def test_run_reports_a_bad_spec_beside_a_project_without_specs(tmp_path):
    """Verify `run` surfaces a spec finding and a project with no specs folder adds nothing."""
    _learnings(tmp_path)
    spec = _project_file(tmp_path, "other", "specs", "s.md", "---\nstatus: done\n---\n# S\n")

    findings = doctor.run(tmp_path, StubEmbedder())

    assert [(f.check, f.path) for f in findings] == [("spec", str(spec))]


def test_nonconformant_names_reports_a_bad_spec_name(tmp_path):
    """Verify a spec named outside the filename rule is reported, since it is never indexed."""
    bad = _project_file(tmp_path, "demo", "specs", "Design Notes.md", "# Notes\n")
    _project_file(tmp_path, "demo", "specs", "2026-10-13-fine.md", "# Fine\n")

    findings = doctor.nonconformant_names(tmp_path, StubEmbedder())

    assert [f.path for f in findings] == [str(bad)]


def test_stale_indexes_reports_a_specs_index_built_under_another_input_version(tmp_path):
    """Verify a specs index stamped with an old input version reads as behind its specs."""
    spec = _project_file(tmp_path, "demo", "specs", "2026-09-01-a.md", "# A\n")
    fieldindex.refresh(spec.parent, StubEmbedder())
    conn = sqlite3.connect(fieldindex.index_path(spec.parent, StubEmbedder()))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    findings = doctor.stale_indexes(tmp_path, StubEmbedder())

    assert [(f.path, f.message) for f in findings] == [
        (str(spec.parent), "index is behind its pages; run: sessionmemory reindex")
    ]
