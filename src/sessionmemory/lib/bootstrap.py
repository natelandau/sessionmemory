"""Create the parts of a vault that cannot be created on demand.

Note directories are not among them. Git does not track an empty directory, so a
pre-created `learnings/` would not survive the vault's first commit, and every note
write already creates its own parent. What has to exist up front is what nothing else
will ever write: the marker that identifies the directory as a vault, the gitignore that
keeps each field's index out of the backup, and a README for the human who opens
the vault in Obsidian.

Nothing here overwrites an existing file. Initialization has to be safe to re-run
against a vault that has been in use for a year, because the reason to re-run it is that
a later version of the CLI adds a file to this list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import tomli_w

from sessionmemory.lib import atomic
from sessionmemory.lib.config import VAULT_MARKER, is_initialized, today
from sessionmemory.lib.embed import MODEL_CODE
from sessionmemory.lib.paths import SYSTEM_DIR

if TYPE_CHECKING:
    from pathlib import Path

# The folder line comes first because git cannot re-include a file whose parent folder
# is ignored, and a global rule such as `local/` would otherwise hide all of it.
LOCAL_IGNORE_LINES: tuple[str, ...] = (
    "!projects/*/local/",
    "!projects/*/local/**",
    "projects/*/local/**/.DS_Store",
    "projects/*/local/**/__pycache__/",
    "projects/*/local/**/node_modules/",
    "projects/*/local/**/.venv/",
    "projects/*/local/**/venv/",
)

_LOCAL_IGNORE_COMMENT = (
    "# A project's local/ folder is private to its person but belongs in the vault. The vault's\n"
    "# own rules outrank a global ignore, so it is re-included over any rule that names it or\n"
    "# its files. Caches and installed dependencies inside it stay out."
)

GITIGNORE = f"""\
# Each field's vector index is derived from the pages beside it and is
# rebuilt by `sessionmemory reindex`. Committing it would churn history on every write.
*.sqlite3
*.sqlite3-journal

# Plans are scratch for the session that writes them and never belong in history.
projects/*/plans/

{_LOCAL_IGNORE_COMMENT}
{chr(10).join(LOCAL_IGNORE_LINES)}

# Obsidian's per-machine window state, which conflicts on every sync.
.obsidian/workspace*.json

.DS_Store
"""

README = f"""\
# Session Memory Vault

One folder per project under `projects/`. Inside each:

- `learnings/` is a field: flat markdown pages, embedded and searchable.
- `logs/` is a second field, one page per session, searched on request.
- `specs/`, `plans/`, `runbooks/`, `reference/`, and `backlog.md` are plain files, never
  indexed. `plans/` is scratch and is never committed: `.gitignore` and the plugin's
  commits both leave it out.
- `backlog.md` is the list of open items for the project: every top-level bullet is an
  open item, under any heading or none. A question for the user goes under `## decide`.
  A finished item is deleted, not ticked.

The `{MODEL_CODE}.sqlite3` file inside a field is its vector index. It is
derived from the pages beside it, gitignored, and rebuilt by `sessionmemory reindex`.

`_system/registry.toml` maps each project's git remote and root to its folder here, and
`_system/vault.toml` is the marker that tells the CLI this directory is a vault.

Pages are created with `sessionmemory new learning` and searched with
`sessionmemory search`, and a backlog item is added with `sessionmemory new backlog`.
Everything else is an ordinary file you read and edit directly.
The format follows the memoryfield spec: https://github.com/calpaterson/memoryfield-spec
"""


@dataclass(frozen=True)
class InitResult:
    """What initialization changed, and what it found already in place."""

    vault: Path
    created: tuple[str, ...]
    existed: tuple[str, ...]


class NotAVaultError(RuntimeError):
    """Raised when a non-empty directory holding no marker is asked to become a vault."""


def is_empty(vault: Path) -> bool:
    """Report whether `vault` holds nothing but dot entries.

    Shared with `commands/_common.require_vault`, which needs the same check to decide
    between naming `sessionmemory init` and `sessionmemory init --force` in its fix-it message.

    Args:
        vault (Path): The directory to check.

    Returns:
        bool: True when the directory has no non-dot entries.
    """
    return all(entry.name.startswith(".") for entry in vault.iterdir())


def _marker_contents() -> str:
    """Build the marker file's contents.

    Returns:
        str: The TOML text for `_system/vault.toml`.
    """
    return tomli_w.dumps({"created": today()})


def ensure_local_ignore(vault: Path) -> bool:
    """Append any missing `local/` lines to the vault's `.gitignore`, creating it if absent.

    Args:
        vault (Path): The vault root.

    Returns:
        bool: True when the file was written, False when every line was already present.
    """
    gitignore = vault / ".gitignore"
    text = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    present = set(text.splitlines())
    # The last matching line wins, so a re-include appended below the exclusions would
    # override them; when it is missing, the exclusions are repeated after it.
    reinclude_missing = LOCAL_IGNORE_LINES[1] not in present
    missing = [
        line
        for index, line in enumerate(LOCAL_IGNORE_LINES)
        if line not in present or (reinclude_missing and index > 1)
    ]
    if not missing:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    atomic.write_text(gitignore, text + "\n".join([_LOCAL_IGNORE_COMMENT, *missing]) + "\n")
    return True


def initialize(vault: Path, *, force: bool = False) -> InitResult:
    """Create every file a vault needs and report what was and was not already there.

    Args:
        vault (Path): The directory to initialize. It is created if absent.
        force (bool): Accept a non-empty directory that holds no vault marker.

    Returns:
        InitResult: The vault-relative names created and the ones already present.

    Raises:
        NotAVaultError: If the directory holds files but no marker and `force` is unset.
    """
    vault.mkdir(parents=True, exist_ok=True)

    marker = f"{SYSTEM_DIR}/{VAULT_MARKER}"
    if not force and not is_empty(vault) and not is_initialized(vault):
        msg = f"{vault} is not empty and has no vault marker; pass --force to initialize it anyway"
        raise NotAVaultError(msg)

    contents: dict[str, str] = {
        marker: _marker_contents(),
        ".gitignore": GITIGNORE,
        "README.md": README,
    }

    created: list[str] = []
    existed: list[str] = []
    for relative_path, text in contents.items():
        destination = vault / relative_path
        if destination.exists():
            existed.append(relative_path)
            continue
        atomic.write_text(destination, text)
        created.append(relative_path)

    return InitResult(vault=vault, created=tuple(sorted(created)), existed=tuple(sorted(existed)))
