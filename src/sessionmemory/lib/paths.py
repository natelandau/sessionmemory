"""Where a project's files live inside the vault.

`learnings/` and `logs/` are fields: flat directories of pages, each with its own
index. `specs/`, `plans/`, and `backlog.md` sit beside them and are never indexed. A
project's files are found by its slug and nothing else; there is no global scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

PROJECTS_DIR = "projects"
SYSTEM_DIR = "_system"

LEARNINGS_DIR = "learnings"
LOGS_DIR = "logs"
SPECS_DIR = "specs"
PLANS_DIR = "plans"
BACKLOG_FILE = "backlog.md"

FIELD_DIRS: tuple[str, ...] = (LEARNINGS_DIR, LOGS_DIR)


def project_dir(vault: Path, slug: str) -> Path:
    """Return the folder holding everything belonging to one project."""
    return vault / PROJECTS_DIR / slug


def learnings_dir(vault: Path, slug: str) -> Path:
    """Return the project's learnings field."""
    return project_dir(vault, slug) / LEARNINGS_DIR


def logs_dir(vault: Path, slug: str) -> Path:
    """Return the project's logs field."""
    return project_dir(vault, slug) / LOGS_DIR


def specs_dir(vault: Path, slug: str) -> Path:
    """Return the project's specs folder."""
    return project_dir(vault, slug) / SPECS_DIR


def plans_dir(vault: Path, slug: str) -> Path:
    """Return the project's plans folder."""
    return project_dir(vault, slug) / PLANS_DIR


def backlog_path(vault: Path, slug: str) -> Path:
    """Return the project's backlog checklist file."""
    return project_dir(vault, slug) / BACKLOG_FILE


def project_paths(vault: Path, slug: str) -> dict[str, str]:
    """Return every path a caller of `sessionmemory project --json` reads, by its payload key.

    The keys are what `sessionmemory project --json` emits and the plugin's hooks read.
    """
    return {
        "project_dir": str(project_dir(vault, slug)),
        "learnings": str(learnings_dir(vault, slug)),
        "logs": str(logs_dir(vault, slug)),
        "specs": str(specs_dir(vault, slug)),
        "plans": str(plans_dir(vault, slug)),
        "backlog": str(backlog_path(vault, slug)),
    }


def iter_project_slugs(vault: Path) -> list[str]:
    """Return every project folder's name, sorted, whether or not it is registered."""
    root = vault / PROJECTS_DIR
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def iter_field_dirs(vault: Path) -> list[Path]:
    """Return every existing field directory in the vault, sorted by project then field."""
    return [
        project_dir(vault, slug) / name
        for slug in iter_project_slugs(vault)
        for name in FIELD_DIRS
        if (project_dir(vault, slug) / name).is_dir()
    ]
