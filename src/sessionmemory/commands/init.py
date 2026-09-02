"""The `init` command that creates a new vault."""

from __future__ import annotations

# Typer resolves annotations from the module's globals to build its CLI, so this
# cannot move into a TYPE_CHECKING block.
from pathlib import Path  # noqa: TC003

import typer
from nclutils import pp

from sessionmemory.commands._common import emit_json
from sessionmemory.lib.bootstrap import InitResult, NotAVaultError, initialize
from sessionmemory.lib.config import VaultNotConfiguredError, vault_root

DIRECTORY_ARGUMENT = typer.Argument(
    None, help="Where to create the vault. Defaults to SESSIONMEMORY_VAULT."
)
FORCE_OPTION = typer.Option(
    False,  # noqa: FBT003
    "--force",
    help="Initialize a directory that already has contents. Nothing existing is overwritten.",
)
JSON_OPTION = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def _report(result: InitResult, *, as_json: bool) -> None:
    """Report what initialization did.

    Args:
        result (InitResult): What `bootstrap.initialize` created and found already there.
        as_json (bool): Emit a machine readable payload instead of prose.
    """
    if as_json:
        emit_json(
            {
                "vault": str(result.vault),
                "created": list(result.created),
                "existed": list(result.existed),
            }
        )
        return

    if result.created:
        pp.success(f"initialized {result.vault}", details=list(result.created))
    else:
        pp.info(f"{result.vault} is already initialized; nothing to create")

    if result.existed:
        pp.info(f"already present: {', '.join(result.existed)}")


def init(
    directory: Path | None = DIRECTORY_ARGUMENT,
    *,
    force: bool = FORCE_OPTION,
    as_json: bool = JSON_OPTION,
) -> None:
    """Create the files a new vault needs.

    A vault cannot be created through `require_vault`, since that helper refuses an
    uninitialized directory and this command is what fixes that. When no directory is
    given, `SESSIONMEMORY_VAULT` is read directly instead.

    Raises:
        Exit: When no directory is given and the environment variable is unset or names
            a missing directory, or when the target directory holds unrelated files and
            `--force` is not given.
    """
    if directory is not None:
        # Every other reader resolves the vault path, so a relative or symlinked path
        # recorded verbatim names a different directory than the one this created.
        vault = directory.expanduser().resolve()
        vault.mkdir(parents=True, exist_ok=True)
    else:
        try:
            vault = vault_root()
        except VaultNotConfiguredError as error:
            pp.error(str(error), details=["export SESSIONMEMORY_VAULT=/path/to/your/vault"])
            raise typer.Exit(1) from error

    try:
        result = initialize(vault, force=force)
    except NotAVaultError as error:
        pp.error(str(error))
        raise typer.Exit(1) from error

    _report(result, as_json=as_json)
