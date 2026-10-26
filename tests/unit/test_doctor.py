"""Tests for the doctor checks."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from sessionmemory.lib import commits, doctor, field, fieldindex, registry
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


def _local_project(vault: Path, root: Path, text: str = "# local\n") -> Path:
    """Register `demo` at a git repository `root` and write its vault copy."""
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    copy = vault / "projects" / "demo" / "local" / "CLAUDE.local.md"
    copy.parent.mkdir(parents=True)
    copy.write_text(text, encoding="utf-8")
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(root))})
    return copy


def test_local_reports_relative_import(tmp_path):
    """Verify a relative @import in the vault copy is reported."""
    vault = tmp_path / "vault"
    vault.mkdir()
    copy = _local_project(vault, tmp_path / "repo", "see @docs/a.md and @/abs.md\n")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [(f.check, f.path, f.message) for f in findings] == [
        ("local", str(copy), "@docs/a.md resolves from the vault folder and will not load")
    ]


def test_local_reports_diverged_copy_at_root(tmp_path):
    """Verify a regular file at the root that differs from the vault copy is reported."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    (root / "CLAUDE.local.md").write_text("other\n", encoding="utf-8")
    (root / ".git" / "info" / "exclude").write_text("CLAUDE.local.md\n", encoding="utf-8")

    findings = doctor.local_instructions(vault, StubEmbedder())

    expected = (
        "CLAUDE.local.md differs from the vault copy; copy the newer one over the vault "
        "copy, delete the checkout copy, and run local sync"
    )
    assert [f.message for f in findings] == [expected]


def test_local_reports_unignored_link(tmp_path):
    """Verify a link git does not ignore is reported."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    copy = _local_project(vault, root)
    (root / "CLAUDE.local.md").symlink_to(copy)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [f.message for f in findings] == [
        "CLAUDE.local.md is not ignored by git; run local sync to add it to info/exclude"
    ]


def test_local_silent_when_linked_and_ignored(tmp_path):
    """Verify an ignored link to the vault copy is not a finding."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    copy = _local_project(vault, root)
    (root / "CLAUDE.local.md").symlink_to(copy)
    (root / ".git" / "info" / "exclude").write_text("CLAUDE.local.md\n", encoding="utf-8")

    assert doctor.local_instructions(vault, StubEmbedder()) == []


def test_local_silent_without_vault_copy(tmp_path):
    """Verify a project with no vault copy is not a finding, whatever sits at its root."""
    vault = tmp_path / "vault"
    (vault / "projects" / "demo").mkdir(parents=True)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "CLAUDE.local.md").write_text("x", encoding="utf-8")
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(root))})

    assert doctor.local_instructions(vault, StubEmbedder()) == []


def test_run_includes_the_local_check():
    """Verify the check runs as part of doctor."""
    assert doctor.local_instructions in doctor.CHECKS


def test_local_silent_when_root_is_not_a_git_repository(tmp_path):
    """Verify a git failure outside a repository is not read as "not ignored"."""
    vault = tmp_path / "vault"
    copy = vault / "projects" / "demo" / "local" / "CLAUDE.local.md"
    copy.parent.mkdir(parents=True)
    copy.write_text("# local\n", encoding="utf-8")
    root = tmp_path / "plain"
    root.mkdir()
    (root / "CLAUDE.local.md").symlink_to(copy)
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(root))})

    assert doctor.local_instructions(vault, StubEmbedder()) == []


def test_local_reports_a_diverged_unignored_file_once(tmp_path):
    """Verify a differing file git does not ignore gets the diverged advice alone."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    (root / "CLAUDE.local.md").write_text("other\n", encoding="utf-8")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert len(findings) == 1
    assert " differs from the vault copy" in findings[0].message


def test_local_advises_adopt_for_an_identical_unignored_file(tmp_path):
    """Verify a regular file equal to the vault copy and not ignored is pointed at adopt."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    (root / "CLAUDE.local.md").write_text("# local\n", encoding="utf-8")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [f.message for f in findings] == [
        (
            "CLAUDE.local.md is not ignored by git; run local sync --adopt CLAUDE.local.md "
            "to replace it with an ignored link"
        )
    ]


def test_local_reports_a_tracked_file(tmp_path):
    """Verify a file git tracks is reported as tracked, not as merely unignored."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    (root / "CLAUDE.local.md").write_text("other\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "CLAUDE.local.md"], cwd=root, check=True)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [f.message for f in findings] == [
        "CLAUDE.local.md is tracked by git; run git rm --cached CLAUDE.local.md before adopting it"
    ]


def test_local_reports_an_unignored_foreign_link(tmp_path):
    """Verify a link to some other file says where it points."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    other = tmp_path / "other.md"
    other.write_text("x\n", encoding="utf-8")
    (root / "CLAUDE.local.md").symlink_to(other)

    findings = doctor.local_instructions(vault, StubEmbedder())

    expected = (
        f"CLAUDE.local.md is not ignored by git, and a symlink to {other.resolve()} rather "
        "than the vault copy; remove it, then run local sync"
    )
    assert [f.message for f in findings] == [expected]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_local_skips_an_unreadable_vault_copy(tmp_path):
    """Verify a vault copy that cannot be read is skipped rather than crashing doctor."""
    vault = tmp_path / "vault"
    vault.mkdir()
    copy = _local_project(vault, tmp_path / "repo", "see @docs/a.md\n")
    copy.chmod(0)
    try:
        assert doctor.local_instructions(vault, StubEmbedder()) == []
    finally:
        copy.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_local_skips_an_unreadable_checkout_file(tmp_path):
    """Verify a checkout file that cannot be read is not judged as differing or equal."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    local = root / "CLAUDE.local.md"
    local.write_text("other\n", encoding="utf-8")
    local.chmod(0)
    try:
        assert doctor.local_instructions(vault, StubEmbedder()) == []
    finally:
        local.chmod(0o644)


def _local_entry(vault: Path, name: str, text: str = "# x\n") -> Path:
    """Write a vault entry for `demo` (a file for `rules/x.md`, a folder holding SKILL.md)."""
    base = vault / "projects" / "demo" / "local" / "claude"
    path = base / name / "SKILL.md" if name.startswith("skills/") else base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_local_flags_relative_import_in_rule(tmp_path):
    """Verify a relative @import in a rule file is reported, and one in a skill is not."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _local_project(vault, tmp_path / "repo")
    rule = _local_entry(vault, "rules/style.md", "see @docs/a.md\n")
    _local_entry(vault, "skills/deploy", "see @docs/b.md\n")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [(f.path, f.message) for f in findings if "resolves" in f.message] == [
        (str(rule), "@docs/a.md resolves from the vault folder and will not load")
    ]


def test_local_flags_old_path_copy(tmp_path):
    """Verify a CLAUDE.local.md left at the old vault path is reported with local sync."""
    vault = tmp_path / "vault"
    vault.mkdir()
    old = vault / "projects" / "demo" / "CLAUDE.local.md"
    old.parent.mkdir(parents=True)
    old.write_text("x\n", encoding="utf-8")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [(f.path, "local sync" in f.message) for f in findings] == [(str(old), True)]


def test_local_flags_diverged_skill_at_root(tmp_path):
    """Verify a skill folder differing from the vault copy is reported by name."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    _local_entry(vault, "skills/deploy-check")
    skill = root / ".claude" / "skills" / "deploy-check"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("other\n", encoding="utf-8")
    exclude = root / ".git" / "info" / "exclude"
    exclude.write_text("/.claude/skills/deploy-check\n", encoding="utf-8")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [(f.path, f.message) for f in findings] == [
        (
            str(skill),
            (
                "skills/deploy-check differs from the vault copy; copy the newer one over "
                "the vault copy, delete the checkout copy, and run local sync"
            ),
        )
    ]


def test_local_flags_unignored_rule_link(tmp_path):
    """Verify an unignored rule link is reported with local link advice."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    target = _local_entry(vault, "rules/style.md")
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "rules" / "style.md").symlink_to(target)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [f.message for f in findings] == [
        "rules/style.md is not ignored by git; run local sync to add it to info/exclude"
    ]


def test_local_names_the_entry_to_adopt_and_to_untrack(tmp_path):
    """Verify adopt and git rm advice name the entry and its checkout path."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    _local_entry(vault, "rules/same.md", "a\n")
    _local_entry(vault, "rules/tracked.md", "b\n")
    rules = root / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "same.md").write_text("a\n", encoding="utf-8")
    (rules / "tracked.md").write_text("c\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".claude/rules/tracked.md"], cwd=root, check=True)

    messages = sorted(f.message for f in doctor.local_instructions(vault, StubEmbedder()))

    assert messages == [
        (
            "rules/same.md is not ignored by git; run local sync --adopt rules/same.md "
            "to replace it with an ignored link"
        ),
        (
            "rules/tracked.md is tracked by git; run git rm --cached "
            ".claude/rules/tracked.md before adopting it"
        ),
    ]


def test_local_flags_a_dangling_link(tmp_path):
    """Verify a dangling symlink at an entry's path is reported with local link advice."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    _local_entry(vault, "rules/style.md")
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "rules" / "style.md").symlink_to(tmp_path / "gone.md")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [f.message for f in findings] == [
        "rules/style.md is a dangling symlink; run local sync to replace it"
    ]


def test_local_reports_an_entry_behind_a_symlinked_claude_once(tmp_path):
    """Verify a symlinked .claude yields one finding and never asks git about the path."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    _local_entry(vault, "rules/style.md")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".claude").symlink_to(elsewhere)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [f.message for f in findings] == [
        "rules/style.md cannot be linked; .claude or .claude/rules is a symlink or not a folder"
    ]


def test_local_keeps_non_git_findings_when_git_fails(tmp_path, monkeypatch):
    """Verify a git failure drops only the tracked and ignored checks."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    old = vault / "projects" / "demo" / "CLAUDE.local.md"
    old.write_text("x\n", encoding="utf-8")
    rule = _local_entry(vault, "rules/style.md", "see @docs/a.md\n")
    (root / "CLAUDE.local.md").write_text("other\n", encoding="utf-8")

    error = doctor.localmd.LocalMdError("git failed")

    def fail(*_args: object, **_kwargs: object) -> set[str]:
        raise error

    monkeypatch.setattr(doctor.localmd, "tracked_names", fail)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert sorted(f.path for f in findings) == sorted([str(old), str(rule)])


def test_local_reports_a_never_linked_skill_behind_a_symlinked_claude(tmp_path):
    """Verify a skill with no link yet is still reported when .claude is a symlink."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    _local_entry(vault, "skills/deploy")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".claude").symlink_to(elsewhere)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [f.message for f in findings] == [
        "skills/deploy cannot be linked; .claude or .claude/skills is a symlink or not a folder"
    ]


def test_local_flags_old_path_copy_that_differs_with_keep_one(tmp_path):
    """Verify an old-path copy differing from local/ says to keep one, not to move it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    old = vault / "projects" / "demo" / "CLAUDE.local.md"
    old.parent.mkdir(parents=True)
    old.write_text("old\n", encoding="utf-8")
    new = vault / "projects" / "demo" / "local" / "CLAUDE.local.md"
    new.parent.mkdir(parents=True)
    new.write_text("new\n", encoding="utf-8")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [(f.path, f.message) for f in findings] == [
        (
            str(old),
            (
                f"CLAUDE.local.md is at its old path and differs from {new}; keep one of "
                "them, delete the other, then run local sync"
            ),
        )
    ]


def test_local_flags_a_wrong_shape_vault_entry(tmp_path):
    """Verify a file among the vault's skills is reported as not a folder."""
    vault = tmp_path / "vault"
    vault.mkdir()
    stray = vault / "projects" / "demo" / "local" / "claude" / "skills" / "README.md"
    stray.parent.mkdir(parents=True)
    stray.write_text("x\n", encoding="utf-8")

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [(f.path, f.message) for f in findings] == [
        (
            str(stray),
            "skills/README.md in the vault is not a folder; fix or remove it in the vault",
        )
    ]


def test_local_does_not_scan_a_non_markdown_rule(tmp_path):
    """Verify a rule file that is not markdown is never scanned for imports."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _local_entry(vault, "rules/notes.txt", "see @docs/a.md\n")

    assert doctor.local_instructions(vault, StubEmbedder()) == []


def test_local_flags_a_git_folder_inside_a_vault_skill(tmp_path):
    """Verify a skill in the vault holding a `.git` folder is reported."""
    vault = tmp_path / "vault"
    vault.mkdir()
    skill = _local_entry(vault, "skills/deploy").parent
    (skill / "vendor" / ".git").mkdir(parents=True)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [(f.path, f.message) for f in findings] == [
        (
            str(skill / "vendor" / ".git"),
            (
                "skills/deploy holds a git repository, which the vault commits as an "
                "embedded repository rather than its files; move it out of the vault"
            ),
        )
    ]


def test_local_survives_an_unreadable_local_folder(tmp_path, monkeypatch):
    """Verify a vault folder that cannot be listed is a finding, not a crash."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _local_entry(vault, "skills/deploy")

    def broken(_co: object) -> list[object]:
        msg = "permission denied"
        raise PermissionError(msg)

    monkeypatch.setattr(doctor.localmd, "vault_entries", broken)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert [f.check for f in findings] == ["local"]
    assert "permission denied" in findings[0].message


def test_run_reports_a_failing_check_and_runs_the_rest(tmp_path, monkeypatch):
    """Verify one check that raises becomes a finding while the other checks still run."""

    def exploding(_vault: Path, _embedder: object) -> list[doctor.Finding]:
        msg = "boom"
        raise OSError(msg)

    def finding(_vault: Path, _embedder: object) -> list[doctor.Finding]:
        return [doctor.Finding("x", "p", "m")]

    monkeypatch.setattr(doctor, "CHECKS", (exploding, finding))

    findings = doctor.run(tmp_path, StubEmbedder())

    assert findings[0].check == "doctor"
    assert "exploding" in findings[0].message
    assert "boom" in findings[0].message
    assert findings[1:] == [doctor.Finding("x", "p", "m")]


def _commits_project(vault: Path, root: Path, lines: dict[str, list[str]]) -> tuple[Path, str]:
    """Register `demo` at a repository with one commit and write one log per entry in `lines`.

    Each value is the list of sha prefixes to list; "HEAD" stands for the real head.
    """
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        cwd=root,
        check=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    logs = _logs(vault)
    for name, shas in lines.items():
        body = "## Summary\n\nWork.\n\n## Changes\n\n" + "".join(
            f"{head[:7] if sha == 'HEAD' else sha} feat: change\n" for sha in shas
        )
        (logs / f"{name}.md").write_text(f'---\ncreated: "{NOW}"\n---\n{body}', encoding="utf-8")
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(root))})
    return logs, head


def test_commits_reports_a_log_with_an_unknown_sha(tmp_path):
    """Verify a log listing a commit no branch contains is reported once."""
    vault = tmp_path / "vault"
    vault.mkdir()
    logs, _ = _commits_project(vault, tmp_path / "repo", {"a": ["deadbee", "HEAD"]})

    findings = doctor.orphaned_commits(vault, StubEmbedder())

    assert [(f.check, f.path) for f in findings] == [("commits", str(logs / "a.md"))]
    assert findings[0].message == "lists 1 commit no branch contains; run: sessionmemory reconcile"


def test_commits_pluralizes_the_count(tmp_path):
    """Verify the message counts several orphaned commits."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _commits_project(vault, tmp_path / "repo", {"a": ["deadbee", "cafe123"]})

    findings = doctor.orphaned_commits(vault, StubEmbedder())

    assert findings[0].message.startswith("lists 2 commits no branch contains")


def test_commits_silent_when_every_sha_is_reachable(tmp_path):
    """Verify a log whose commits are all on a branch is not reported."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _commits_project(vault, tmp_path / "repo", {"a": ["HEAD"], "b": []})

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def test_commits_skips_a_root_that_is_not_a_repository(tmp_path):
    """Verify a root that is a plain directory is skipped."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "plain"
    root.mkdir()
    logs = _logs(vault)
    (logs / "a.md").write_text(
        f'---\ncreated: "{NOW}"\n---\n## Changes\n\ndeadbee feat: gone\n', encoding="utf-8"
    )
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(root))})

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def test_commits_skips_a_root_inside_another_repository(tmp_path):
    """Verify a root that is a plain subdirectory of a repository is skipped."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _commits_project(vault, tmp_path / "repo", {"a": ["deadbee"]})
    sub = tmp_path / "repo" / "sub"
    sub.mkdir()
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(sub))})

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def test_commits_does_not_count_an_ambiguous_sha(tmp_path, monkeypatch):
    """Verify an ambiguous sha may be on trunk, so it is not reported."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _commits_project(vault, tmp_path / "repo", {"a": ["deadbee"]})
    monkeypatch.setattr(
        commits.Repository,
        "expand",
        lambda _self, _shas: commits.Expansion(full={}, ambiguous=frozenset({"deadbee"})),
    )

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def test_commits_skips_a_project_when_git_fails(tmp_path, monkeypatch):
    """Verify a git failure in one project is skipped, not raised."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _commits_project(vault, tmp_path / "repo", {"a": ["deadbee"]})

    def fail(_self: object, *_args: object) -> None:
        raise commits.CommitsError(command="git x", detail="boom")

    monkeypatch.setattr(commits.Repository, "expand", fail)

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def test_commits_runs_no_git_when_logs_list_no_commits(tmp_path, monkeypatch):
    """Verify a project whose logs list no commits never reaches git."""
    vault = tmp_path / "vault"
    vault.mkdir()
    _commits_project(vault, tmp_path / "repo", {"a": []})

    def boom(*_args: object) -> None:
        pytest.fail("git was run")

    monkeypatch.setattr(commits.Repository, "is_repository", boom)
    monkeypatch.setattr(commits.Repository, "expand", boom)

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def test_doctor_run_includes_commits():
    """Verify the commits check is registered."""
    assert doctor.orphaned_commits in doctor.CHECKS


def test_commits_does_not_count_unknown_shas_in_a_shallow_clone(tmp_path):
    """Verify a shallow clone's unknown shas may sit below the cut, so they are not counted."""
    vault = tmp_path / "vault"
    vault.mkdir()
    full = tmp_path / "repo"
    _commits_project(vault, full, {"a": ["deadbee"]})
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "y",
        ],
        cwd=full,
        check=True,
    )
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{full}", str(shallow)],
        cwd=tmp_path,
        check=True,
    )
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(shallow))})

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def test_commits_does_not_count_unknown_shas_in_a_single_branch_clone(tmp_path):
    """Verify a single-branch clone's unknown shas may sit on an unfetched branch."""
    vault = tmp_path / "vault"
    vault.mkdir()
    full = tmp_path / "repo"
    _commits_project(vault, full, {"a": ["deadbee"]})
    single = tmp_path / "single"
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", f"file://{full}", str(single)],
        cwd=tmp_path,
        check=True,
    )
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(single))})

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def test_commits_does_not_count_a_known_orphan_in_a_shallow_clone(tmp_path):
    """Verify doctor never flags a commit that reconcile would leave unchecked."""
    vault = tmp_path / "vault"
    vault.mkdir()
    full = tmp_path / "repo"
    logs, _ = _commits_project(vault, full, {"a": []})
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{full}", str(shallow)],
        cwd=tmp_path,
        check=True,
    )
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "checkout", "-q", "-b", "scratch"], cwd=shallow, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "o"], cwd=shallow, check=True)
    orphan = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=shallow, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run([*git, "checkout", "-q", "-"], cwd=shallow, check=True)
    subprocess.run([*git, "branch", "-q", "-D", "scratch"], cwd=shallow, check=True)
    (logs / "a.md").write_text(
        f'---\ncreated: "{NOW}"\n---\n## Changes\n\n{orphan[:7]} feat: o\n', encoding="utf-8"
    )
    registry.save(vault, {"demo": registry.Project(slug="demo", remotes=(), root=str(shallow))})

    assert doctor.orphaned_commits(vault, StubEmbedder()) == []


def _settings_copies(vault: Path, root: Path, *, checkout: str | None, theirs: str | None) -> Path:
    """Write the checkout's and the vault's settings.local.json for `demo`; return the link."""
    link = root / ".claude" / "settings.local.json"
    if checkout is not None:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text(checkout, encoding="utf-8")
    if theirs is not None:
        target = vault / "projects" / "demo" / "local" / "claude" / "settings.local.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(theirs, encoding="utf-8")
    return link


@pytest.mark.parametrize(
    ("ours", "theirs", "expected"),
    [
        (
            '{"a": 1}',
            '{"a": 2}',
            (
                "settings.local.json differs from the vault copy; run local sync to choose "
                "which copy to keep"
            ),
        ),
        (
            '{"a": 1}',
            None,
            "settings.local.json is only in the checkout; run local sync to keep a copy",
        ),
        (
            None,
            '{"a": 1}',
            "settings.local.json is only in the vault; run local sync to keep a copy",
        ),
    ],
)
def test_doctor_settings_pending_states(tmp_path, ours, theirs, expected):
    """Verify each settings state that waits on the person is reported with local sync."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    link = _settings_copies(vault, root, checkout=ours, theirs=theirs)

    findings = [
        f for f in doctor.local_instructions(vault, StubEmbedder()) if "settings" in f.message
    ]

    assert [(f.path, f.message) for f in findings] == [(str(link), expected)]


def test_doctor_settings_same_is_quiet(tmp_path):
    """Verify matching settings copies produce no finding."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    _settings_copies(vault, root, checkout='{"a": 1}', theirs='{"a":1}')

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert not [f for f in findings if "settings" in f.message]


def test_doctor_settings_linked_tracked_and_blocked(tmp_path):
    """Verify a symlinked, a tracked, and a blocked settings file each get their own advice."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    link = _settings_copies(vault, root, checkout=None, theirs="{}")
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    link.parent.mkdir(parents=True)
    link.symlink_to(other)

    def settings_messages() -> list[str]:
        found = doctor.local_instructions(vault, StubEmbedder())
        return [f.message for f in found if "settings" in f.message]

    assert settings_messages() == [
        (
            "settings.local.json is a symlink, which Claude Code cannot write through; "
            "replace it with a regular file"
        )
    ]

    link.unlink()
    link.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "-f", ".claude/settings.local.json"], cwd=root, check=True)
    assert settings_messages() == [
        (
            "settings.local.json is tracked by git; run git rm --cached "
            ".claude/settings.local.json to keep it private"
        )
    ]

    subprocess.run(
        ["git", "rm", "-q", "--cached", ".claude/settings.local.json"], cwd=root, check=True
    )
    shutil.rmtree(root / ".claude")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".claude").symlink_to(elsewhere)
    assert settings_messages() == [
        "settings.local.json cannot be synced; .claude is a symlink or not a folder"
    ]


def test_doctor_messages_name_local_sync(tmp_path):
    """Verify no local finding tells the person to run the removed link or adopt commands."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    target = _local_entry(vault, "rules/style.md")
    (root / ".claude" / "rules").mkdir(parents=True)
    (root / ".claude" / "rules" / "style.md").symlink_to(target)
    (root / ".claude" / "skills").mkdir()
    (root / ".claude" / "skills" / "gone").symlink_to(tmp_path / "missing")
    legacy = vault / "projects" / "demo" / "CLAUDE.local.md"
    legacy.write_text("old\n", encoding="utf-8")

    messages = [f.message for f in doctor.local_instructions(vault, StubEmbedder())]

    assert messages
    assert not [m for m in messages if "local link" in m or "local adopt" in m]


def test_doctor_skips_settings_when_git_cannot_say_tracked(tmp_path, monkeypatch):
    """Verify an unknown tracked state reports nothing for settings rather than a guess."""
    vault = tmp_path / "vault"
    vault.mkdir()
    root = tmp_path / "repo"
    _local_project(vault, root)
    _settings_copies(vault, root, checkout='{"a": 1}', theirs='{"a": 2}')

    def broken(*_args: object, **_kwargs: object) -> set[str]:
        msg = "git ls-files failed"
        raise doctor.localmd.LocalMdError(msg)

    monkeypatch.setattr(doctor.localmd, "tracked_names", broken)

    findings = doctor.local_instructions(vault, StubEmbedder())

    assert not [f for f in findings if "settings" in f.message]
