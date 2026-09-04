"""Locate the vault."""

from __future__ import annotations

import datetime
import os
import tomllib
from pathlib import Path

from sessionmemory.lib.paths import SYSTEM_DIR

VAULT_ENV_VAR = "SESSIONMEMORY_VAULT"

# The plugin's settings file, read here only for `[vault] root`.
CONFIG_FILE = Path("~/.claude/sessionmemory.toml")

VAULT_MARKER = "vault.toml"


class VaultNotConfiguredError(RuntimeError):
    """Raised when the vault location is unset or does not exist."""


def vault_root() -> Path:
    """Return the vault directory named by the environment or the config file.

    `SESSIONMEMORY_VAULT` wins. When it is unset, `[vault] root` in `CONFIG_FILE` is read
    instead, so a plugin user who recorded the root there once has the CLI and the hooks
    agree on where the vault is. The precedence is the hooks' own.

    The path is resolved, so every directory the CLI prints is absolute, such as the
    `project_dir` in `sessionmemory project --json`. A relative value would otherwise be
    printed as given, and usable only from the directory the variable was written for.

    Returns:
        Path: The resolved vault directory.

    Raises:
        VaultNotConfiguredError: If neither source names a vault, the config file cannot
            be parsed, or the named directory is missing.
    """
    raw = os.environ.get(VAULT_ENV_VAR)
    source = VAULT_ENV_VAR
    if not raw:
        raw = _configured_root()
        source = str(CONFIG_FILE)
    if not raw:
        msg = f"{VAULT_ENV_VAR} is not set. Point it at your vault repository."
        raise VaultNotConfiguredError(msg)

    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        msg = f"{source} names {root}, which does not exist or is not a directory."
        raise VaultNotConfiguredError(msg)

    return root


def _configured_root() -> str | None:
    """Return `[vault] root` from the config file, or None when it names nothing.

    A value that is not a string is treated as absent, as the hooks treat it. An
    unparsable file raises rather than reading as absent, because a person at a keyboard
    fixes a named parse error faster than a "not set" message that is not true.

    Raises:
        VaultNotConfiguredError: If the file exists but cannot be read or parsed.
    """
    path = CONFIG_FILE.expanduser()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as error:
        msg = f"{CONFIG_FILE} could not be read: {error}"
        raise VaultNotConfiguredError(msg) from error

    vault = data.get("vault")
    if not isinstance(vault, dict):
        return None
    root = vault.get("root")
    return root if isinstance(root, str) and root else None


def today() -> str:
    """Return today's local date as an ISO string.

    A date a person reads, in a filename or a checklist line, follows their clock; only
    the frontmatter timestamps from `now` are UTC, which the memoryfield spec asks for.

    Returns:
        str: Today's date.
    """
    return datetime.datetime.now().astimezone().date().isoformat()


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
