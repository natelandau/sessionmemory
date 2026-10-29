"""Where a project's files live inside the vault.

`learnings/` and `logs/` are fields: flat directories of pages, each with its own
index. `specs/` is indexed too, with the same table but a derived embedding input, though
it is not a field. `plans/`, `runbooks/`, `reference/`, `backlog.md`, and `local/` sit
beside them and are never indexed. A project's files are found by its slug and nothing
else; there is no global scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PROJECTS_DIR = "projects"
SYSTEM_DIR = "_system"

LEARNINGS_DIR = "learnings"
LOGS_DIR = "logs"
SPECS_DIR = "specs"
PLANS_DIR = "plans"
RUNBOOKS_DIR = "runbooks"
REFERENCE_DIR = "reference"
BACKLOG_FILE = "backlog.md"
LOCAL_DIR = "local"
LOCAL_MD_FILE = "CLAUDE.local.md"

FIELD_DIRS: tuple[str, ...] = (LEARNINGS_DIR, LOGS_DIR)
INDEX_DIRS: tuple[str, ...] = (*FIELD_DIRS, SPECS_DIR)


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


def runbooks_dir(vault: Path, slug: str) -> Path:
    """Return the project's runbooks folder."""
    return project_dir(vault, slug) / RUNBOOKS_DIR


def reference_dir(vault: Path, slug: str) -> Path:
    """Return the project's reference folder."""
    return project_dir(vault, slug) / REFERENCE_DIR


def backlog_path(vault: Path, slug: str) -> Path:
    """Return the project's backlog checklist file."""
    return project_dir(vault, slug) / BACKLOG_FILE


def local_dir(vault: Path, slug: str) -> Path:
    """Return the folder holding the project's private files kept for its checkouts."""
    return project_dir(vault, slug) / LOCAL_DIR


def legacy_local_md_path(vault: Path, slug: str) -> Path:
    """Return where the vault copy of CLAUDE.local.md lived before `local/` existed."""
    return project_dir(vault, slug) / LOCAL_MD_FILE


_PROJECT_PATHS: dict[str, Callable[[Path, str], Path]] = {
    "project_dir": project_dir,
    LEARNINGS_DIR: learnings_dir,
    LOGS_DIR: logs_dir,
    SPECS_DIR: specs_dir,
    PLANS_DIR: plans_dir,
    RUNBOOKS_DIR: runbooks_dir,
    REFERENCE_DIR: reference_dir,
    "backlog": backlog_path,
    LOCAL_DIR: local_dir,
}
PROJECT_PATH_KEYS: tuple[str, ...] = tuple(_PROJECT_PATHS)


def project_paths(vault: Path, slug: str) -> dict[str, str]:
    """Return every path a caller of `sessionmemory project --json` reads, by its payload key.

    The keys are what `sessionmemory project --json` emits and the plugin's hooks read.
    """
    return {key: str(locate(vault, slug)) for key, locate in _PROJECT_PATHS.items()}


def index_dirs(vault: Path, slug: str) -> list[Path]:
    """Return one project's indexed directories, learnings, logs, then specs, existing or not."""
    return [project_dir(vault, slug) / name for name in INDEX_DIRS]


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


def iter_index_dirs(vault: Path) -> list[Path]:
    """Return every existing indexed directory, sorted by project then learnings, logs, specs."""
    return [
        directory
        for slug in iter_project_slugs(vault)
        for directory in index_dirs(vault, slug)
        if directory.is_dir()
    ]
