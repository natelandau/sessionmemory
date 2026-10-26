"""Tests for the manifest of paths adopted into a project's `local/` folder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sessionmemory.lib import localmanifest

if TYPE_CHECKING:
    from pathlib import Path


def test_read_missing_manifest_is_empty(tmp_path: Path) -> None:
    """Verify a folder with no manifest lists nothing and reports no problem."""
    assert localmanifest.read(tmp_path) == localmanifest.Manifest(paths=(), problems=())


def test_add_writes_sorted_list_and_reads_back(tmp_path: Path) -> None:
    """Verify add keeps the list sorted, skips a repeat, and writes the header."""
    assert localmanifest.add(tmp_path, ".claude/z.toml") is True
    assert localmanifest.add(tmp_path, ".claude/a.toml") is True
    assert localmanifest.add(tmp_path, ".claude/a.toml") is False

    assert localmanifest.read(tmp_path).paths == (".claude/a.toml", ".claude/z.toml")
    assert "local sync --adopt" in (tmp_path / "entries.toml").read_text()


def test_read_reports_bad_lines_and_keeps_good_ones(tmp_path: Path) -> None:
    """Verify each bad or repeated item is a problem while valid paths survive."""
    (tmp_path / "entries.toml").write_text(
        'paths = [".claude/a.toml", "../x", "skills/foo", ".claude/a.toml", 3]\n'
    )

    manifest = localmanifest.read(tmp_path)

    assert manifest.paths == (".claude/a.toml",)
    assert len(manifest.problems) == 4


def test_read_unparsable_manifest_is_one_problem(tmp_path: Path) -> None:
    """Verify a manifest that is not TOML lists nothing and reports one problem."""
    (tmp_path / "entries.toml").write_text("paths = [\n")

    manifest = localmanifest.read(tmp_path)

    assert manifest.paths == ()
    assert len(manifest.problems) == 1


def test_read_paths_not_a_list_is_a_problem(tmp_path: Path) -> None:
    """Verify a `paths` value that is not a list is reported."""
    (tmp_path / "entries.toml").write_text("paths = 3\n")

    assert localmanifest.read(tmp_path).problems


def test_add_refuses_when_manifest_has_problems(tmp_path: Path) -> None:
    """Verify add never rewrites a manifest it cannot read cleanly."""
    (tmp_path / "entries.toml").write_text("paths = 3\n")

    with pytest.raises(localmanifest.ManifestError):
        localmanifest.add(tmp_path, ".claude/a.toml")

    assert (tmp_path / "entries.toml").read_text() == "paths = 3\n"


@pytest.mark.parametrize(
    ("relpath", "expected"),
    [
        (".claude/natelandau-toolkit.toml", "claude/natelandau-toolkit.toml"),
        (".claude/agents", "claude/agents"),
        (".Claude/agents", "claude/agents"),
        ("notes/private.md", "notes/private.md"),
    ],
)
def test_vault_relpath(relpath: str, expected: str) -> None:
    """Verify a leading `.claude/` becomes `claude/` and any other path is kept."""
    assert localmanifest.vault_relpath(relpath) == expected


@pytest.mark.parametrize(
    "relpath",
    [
        "",
        "/abs",
        "a//b",
        "./a",
        "a/../b",
        "..",
        ".git",
        ".git/config",
        ".claude",
        ".claude/skills",
        ".claude/rules",
        ".claude/skills/x",
        ".claude/rules/x/y.md",
        ".claude/settings.json",
        ".claude/settings.local.json",
        "claude",
        "claude/x",
        "entries.toml",
        "CLAUDE.local.md",
        "skills/foo",
        "rules/foo",
        "claude.local.md",
        ".Claude/skills/x",
        "Claude/x",
        ".GIT/config",
        "Entries.toml",
        "settings.local.json",
        "Settings.local.json",
    ],
)
def test_name_problem_refuses(relpath: str) -> None:
    """Verify every path the spec refuses gets a reason."""
    assert localmanifest.name_problem(relpath) is not None


@pytest.mark.parametrize(
    "relpath",
    [".claude/natelandau-toolkit.toml", ".claude/agents", "notes/private.md", "skills/a/b"],
)
def test_name_problem_accepts(relpath: str) -> None:
    """Verify an ordinary path has no problem."""
    assert localmanifest.name_problem(relpath) is None


@pytest.mark.parametrize(
    ("relpath", "expected"),
    [
        ("CLAUDE.local.md", True),
        ("skills/foo", True),
        ("rules/foo.md", True),
        ("skills/foo/bar", False),
        (".claude/skills/foo", False),
        ("notes/a.md", False),
    ],
)
def test_is_kind_name(relpath: str, expected: bool) -> None:
    """Verify only the names `--adopt` already gives a meaning count as kind names."""
    assert localmanifest.is_kind_name(relpath) is expected


@pytest.mark.parametrize(
    "listed",
    [
        [".claude/agents", ".claude/agents/foo.md"],
        [".claude/agents/foo.md", ".claude/agents"],
    ],
)
def test_read_drops_a_path_nested_in_another(tmp_path: Path, listed: list[str]) -> None:
    """Verify of two nested lines only the outer one is kept, and the inner one is reported."""
    quoted = ", ".join(f'"{item}"' for item in listed)
    (tmp_path / "entries.toml").write_text(f"paths = [{quoted}]\n")

    manifest = localmanifest.read(tmp_path)

    assert manifest.paths == (".claude/agents",)
    assert len(manifest.problems) == 1
    assert ".claude/agents/foo.md" in manifest.problems[0]


def test_read_treats_case_variants_as_repeats(tmp_path: Path) -> None:
    """Verify two lines differing only in case count as one path listed twice."""
    (tmp_path / "entries.toml").write_text('paths = [".claude/a.toml", ".claude/A.toml"]\n')

    manifest = localmanifest.read(tmp_path)

    assert manifest.paths == (".claude/a.toml",)
    assert len(manifest.problems) == 1


def test_read_problems_do_not_repeat_the_file_path(tmp_path: Path) -> None:
    """Verify a problem line names the bad item, not the manifest's own path."""
    (tmp_path / "entries.toml").write_text('paths = ["../x"]\n')

    assert str(tmp_path) not in localmanifest.read(tmp_path).problems[0]


def test_add_round_trips_non_bmp_path(tmp_path: Path) -> None:
    """Verify a path holding a character outside the BMP survives a write and read."""
    localmanifest.add(tmp_path, "notes/\U0001f600.md")

    assert localmanifest.read(tmp_path) == localmanifest.Manifest(
        paths=("notes/\U0001f600.md",), problems=()
    )


def test_add_refuses_case_variant_of_listed_path(tmp_path: Path) -> None:
    """Verify add refuses a path that differs from a listed one only in case."""
    localmanifest.add(tmp_path, "docs")

    with pytest.raises(localmanifest.ManifestError, match="only in case"):
        localmanifest.add(tmp_path, "Docs")

    assert localmanifest.read(tmp_path).paths == ("docs",)


def test_discard_drops_listed_path(tmp_path: Path) -> None:
    """Verify discard removes one listed path and leaves the rest."""
    localmanifest.add(tmp_path, "a")
    localmanifest.add(tmp_path, "b")

    localmanifest.discard(tmp_path, "a")

    assert localmanifest.read(tmp_path).paths == ("b",)


def test_read_parses_an_unchanged_manifest_once(tmp_path: Path, monkeypatch) -> None:
    """Verify repeated reads of an unchanged manifest parse it once, and a write is seen."""
    localmanifest.add(tmp_path, ".claude/a.toml")
    calls: list[str] = []
    real = localmanifest.tomllib.loads

    def counting(text: str) -> dict[str, object]:
        calls.append(text)
        return real(text)

    monkeypatch.setattr(localmanifest.tomllib, "loads", counting)

    localmanifest.read(tmp_path)
    localmanifest.read(tmp_path)
    assert len(calls) == 1

    localmanifest.add(tmp_path, ".claude/b.toml")
    assert localmanifest.read(tmp_path).paths == (".claude/a.toml", ".claude/b.toml")
