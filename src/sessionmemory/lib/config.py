"""Locate the vault."""

from __future__ import annotations

import datetime
import os
import tomllib
from pathlib import Path

from sessionmemory.lib.paths import SYSTEM_DIR

VAULT_ENV_VAR = "SESSIONMEMORY_VAULT"

VAULT_MARKER = "vault.toml"


class VaultNotConfiguredError(RuntimeError):
    """Raised when the vault location is unset or does not exist."""


def vault_root() -> Path:
    """Return the vault directory named by the environment.

    The path is resolved, so every directory the CLI prints is absolute, such as the
    `project_dir` in `sessionmemory project --json`. A relative value would otherwise be
    printed as given, and usable only from the directory the variable was written for.

    Returns:
        Path: The resolved vault directory.

    Raises:
        VaultNotConfiguredError: If the variable is unset or names a missing directory.
    """
    raw = os.environ.get(VAULT_ENV_VAR)
    if not raw:
        msg = f"{VAULT_ENV_VAR} is not set. Point it at your vault repository."
        raise VaultNotConfiguredError(msg)

    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        msg = f"{VAULT_ENV_VAR} is {root}, which does not exist or is not a directory."
        raise VaultNotConfiguredError(msg)

    return root


def today() -> str:
    """Return today's date as an ISO string.

    Returns:
        str: Today's date.
    """
    return datetime.datetime.now(tz=datetime.UTC).date().isoformat()


def now() -> str:
    """Return the current UTC time as the quoted-string ISO form the memoryfield spec wants.

    Second precision and a `Z` suffix: PyYAML quotes a value of this shape on dump, which
    is what keeps a YAML 1.1 parser from coercing it to a datetime on the way back in.
    """
    stamp = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0)
    return stamp.isoformat().replace("+00:00", "Z")


def marker_path(vault: Path) -> Path:
    """Return the path of the file that proves a directory is a vault.

    Args:
        vault (Path): The vault root.

    Returns:
        Path: The marker file, whether or not it exists.
    """
    return vault / SYSTEM_DIR / VAULT_MARKER


def is_initialized(vault: Path) -> bool:
    """Report whether `vault` has been initialized by `sessionmemory init`.

    A directory that merely exists is not a vault. `SESSIONMEMORY_VAULT` pointing at a
    home directory or a mistyped path would otherwise be accepted, and the first note
    written would scatter a `learnings/` tree into it.

    A marker that cannot be parsed is treated as absent rather than as proof, so a
    truncated file produces "run sessionmemory init" instead of an error from deep inside a
    later command.

    Args:
        vault (Path): The vault root.

    Returns:
        bool: True when a readable marker file is present.
    """
    path = marker_path(vault)
    if not path.is_file():
        return False
    try:
        tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return False
    return True
