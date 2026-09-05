"""Tests for the project layout."""

from __future__ import annotations

from sessionmemory.lib import paths


def test_project_paths_names_every_location(tmp_path):
    """Verify the payload carries the keys the hooks read, and backlog is a file."""
    result = paths.project_paths(tmp_path, "demo")

    assert result == {
        "project_dir": str(tmp_path / "projects" / "demo"),
        "learnings": str(tmp_path / "projects" / "demo" / "learnings"),
        "logs": str(tmp_path / "projects" / "demo" / "logs"),
        "specs": str(tmp_path / "projects" / "demo" / "specs"),
        "plans": str(tmp_path / "projects" / "demo" / "plans"),
        "backlog": str(tmp_path / "projects" / "demo" / "backlog.md"),
    }


def test_iter_project_slugs_lists_folders_sorted(tmp_path):
    """Verify slugs come from the projects folder, sorted, ignoring files."""
    (tmp_path / "projects" / "beta").mkdir(parents=True)
    (tmp_path / "projects" / "alpha").mkdir()
    (tmp_path / "projects" / "stray.md").write_text("")

    assert paths.iter_project_slugs(tmp_path) == ["alpha", "beta"]


def test_iter_project_slugs_without_projects_folder(tmp_path):
    """Verify a fresh vault lists no projects."""
    assert paths.iter_project_slugs(tmp_path) == []


def test_iter_field_dirs_lists_every_existing_field(tmp_path):
    """Verify every project's learnings and logs folder is listed, sorted, and a missing one skipped."""
    (tmp_path / "projects" / "beta" / "logs").mkdir(parents=True)
    (tmp_path / "projects" / "alpha" / "learnings").mkdir(parents=True)
    (tmp_path / "projects" / "alpha" / "logs").mkdir()
    (tmp_path / "projects" / "alpha" / "specs").mkdir()

    assert paths.iter_field_dirs(tmp_path) == [
        tmp_path / "projects" / "alpha" / "learnings",
        tmp_path / "projects" / "alpha" / "logs",
        tmp_path / "projects" / "beta" / "logs",
    ]
