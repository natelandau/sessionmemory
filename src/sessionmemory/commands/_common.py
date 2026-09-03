"""Helpers shared by every command module."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer
from nclutils import pp

from sessionmemory.lib import embed, registry
from sessionmemory.lib.bootstrap import is_empty
from sessionmemory.lib.config import (
    CONFIG_FILE,
    VaultNotConfiguredError,
    is_initialized,
    vault_root,
)
from sessionmemory.lib.paths import SYSTEM_DIR
from sessionmemory.lib.resolve import resolve as resolve_project

if TYPE_CHECKING:
    from sessionmemory.lib.embed import Embedder

EMBEDDER_ENV_VAR = "SESSIONMEMORY_EMBEDDER"


# The two ways to name the vault, offered together whenever neither is set.
VAULT_FIX = [
    "export SESSIONMEMORY_VAULT=/path/to/your/vault",
    f"or set [vault] root in {CONFIG_FILE}",
]


def build_embedder() -> Embedder:
    """Build the embedder commands use: the stub under test, the real model otherwise.

    Read at call time so a test selects the stub with `monkeypatch.setenv`.
    """
    if os.environ.get(EMBEDDER_ENV_VAR) == "stub":
        return embed.StubEmbedder()
    return embed.default_embedder()


STDIN_SENTINEL = "-"


def resolve_body(body: str, body_file: Path | None) -> str:
    """Return a note's markdown body, read from a file or stdin when one was named.

    Shell-quoting a document of any real length into `--body` is impractical, so without
    this a caller creates the note empty and then writes the file itself, which is the
    one habit every skill in this project tells a caller not to form.

    Args:
        body (str): The `--body` value.
        body_file (Path | None): The `--body-file` value, where `-` means stdin.

    Returns:
        str: The body to write.

    Raises:
        Exit: When both options carry a value, or the named file cannot be read.
    """
    if body_file is None:
        return body

    if body:
        fail("--body and --body-file are mutually exclusive", ["pass one or the other"])

    if str(body_file) == STDIN_SENTINEL:
        return sys.stdin.read()

    try:
        return body_file.read_text(encoding="utf-8")
    except OSError as error:
        fail(f"cannot read {body_file}: {error.strerror or error}")
    except UnicodeDecodeError:
        fail(f"{body_file} is not valid UTF-8 text")


def emit_value(text: str) -> None:
    """Print one machine readable value with nothing added to it.

    Machine readable output is read by a caller rather than by a person, so an ANSI escape
    in it is corruption rather than decoration: a styled path breaks the page path `vault
    search` prints for a caller to copy, and a styled version string breaks the comparison
    it exists for. Rich styles values by default and honors `FORCE_COLOR`, which real
    shells export, so styling does not depend on stdout being a terminal and a caller
    cannot opt out of it. Every value a caller consumes goes through here; prose for a
    person keeps its styling and goes through `pp.success`, `pp.warning`, and `pp.error`.

    `markup=False` keeps square brackets from being read as Rich tags, `emoji=False`
    keeps `:word:` from being replaced by the emoji of that name, `highlight=False`
    suppresses the automatic value coloring, and `soft_wrap=True` keeps a value longer
    than the console width on one line.

    Args:
        text (str): The value to print.
    """
    pp.console().print(text, markup=False, emoji=False, highlight=False, soft_wrap=True)


def emit_json(payload: object) -> None:
    """Print a machine readable payload as unstyled JSON.

    `--json` exists so a caller can read a value out of the output instead of scraping
    prose, which makes an escape sequence anywhere in the payload a parse error. Rich's
    JSON printer syntax-highlights what it writes, so the payload is serialized here and
    printed through `emit_value`, which documents why nothing may style it.

    Args:
        payload (object): Any JSON-serializable value.
    """
    emit_value(json.dumps(payload, indent=2))


def require_vault() -> Path:
    """Return an initialized vault root, or exit with the command that fixes it.

    Every command needs the vault location before it can do anything else, so this is
    the one place that turns an unset, missing, or uninitialized `SESSIONMEMORY_VAULT`
    into a clean message instead of a traceback or a note scattered into the wrong place.

    Returns:
        Path: The resolved, initialized vault directory.

    Raises:
        Exit: When the variable is unset, names a missing directory, or names a
            directory that `sessionmemory init` has never touched.
    """
    try:
        vault = vault_root()
    except VaultNotConfiguredError as error:
        pp.error(str(error), details=VAULT_FIX)
        raise typer.Exit(1) from error

    if not is_initialized(vault):
        # An empty directory is the ordinary bootstrap case, and `sessionmemory init` is safe
        # for a caller, human or agent, to run there. A non-empty directory holding no
        # marker is different: initializing it is a one-time human decision about a
        # specific path, never something to hand an agent as its next action, so that
        # case is described rather than phrased as a `run:` instruction to follow.
        if is_empty(vault):
            pp.error(f"{vault} is not a vault", details=["run: sessionmemory init"])
            raise typer.Exit(1)

        pp.error(
            f"{vault} is not an initialized vault",
            details=[
                "it holds files but no _system/vault.toml marker",
                (
                    "if this is an existing vault, a human can initialize it once by "
                    "hand with: sessionmemory init --force"
                ),
            ],
        )
        raise typer.Exit(1)

    return vault


def fail(message: str, details: list[str] | None = None) -> NoReturn:
    """Report a failure and exit non-zero.

    Args:
        message (str): The error line.
        details (list[str] | None): Follow-up lines.

    Raises:
        Exit: Always, with code 1.
    """
    pp.error(message, details=details if details is not None else [])
    raise typer.Exit(1)


def report_malformed_registry(vault: Path, error: registry.RegistryError) -> NoReturn:
    """Report a malformed registry.toml and exit non-zero.

    Args:
        vault (Path): The vault root.
        error (registry.RegistryError): The error raised while loading the registry.

    Raises:
        Exit: Always, with code 1.
    """
    path = vault / SYSTEM_DIR / registry.REGISTRY_FILE
    pp.error(f"{path} is malformed: {error}")
    raise typer.Exit(1) from error


def require_project(vault: Path, cwd: Path | None = None) -> str:
    """Return the current project's slug, or exit with instructions.

    Args:
        vault (Path): The vault root.
        cwd (Path | None): The directory to resolve, or None for the shell's.

    Returns:
        str: The resolved project slug.

    Raises:
        Exit: When the working directory is not a registered project.
    """
    try:
        result = resolve_project(vault, (cwd if cwd is not None else Path.cwd()).resolve())
    except registry.RegistryError as error:
        report_malformed_registry(vault, error)

    if not result.registered or result.slug is None:
        pp.error(
            "this directory is not a registered project",
            details=["run: sessionmemory project --register"],
        )
        raise typer.Exit(1)
    return result.slug
