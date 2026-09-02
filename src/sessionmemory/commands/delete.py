"""The `delete` command: remove pages and files from the vault permanently.

No confirmation and no dry run. The vault is a git repository, so a regretted delete is
recovered from history, and a page that is wrong or spent has no other end.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import typer
from nclutils import pp

from sessionmemory.commands._common import build_embedder, emit_json, fail, require_vault
from sessionmemory.lib import fieldindex, paths

if TYPE_CHECKING:
    from sessionmemory.lib.embed import Embedder

PATHS = typer.Argument(..., help="Files to delete.")
JSON = typer.Option(False, "--json", help="Emit JSON instead of prose.")  # noqa: FBT003


def _check_vault_boundary(targets: list[Path], vault: Path) -> None:
    """Verify all targets are inside the vault; fail if any is outside."""
    for target in targets:
        resolved = target.resolve()
        if not resolved.is_relative_to(vault):
            fail(f"{target} is outside the vault", ["nothing was deleted"])


def _delete_targets(targets: list[Path]) -> list[dict[str, object]]:
    """Delete each target and forget its index row if applicable."""
    embedder: Embedder | None = None
    records: list[dict[str, object]] = []
    for target in targets:
        # Unlink the path as given, not its resolved target, so deleting a
        # symlink removes the link rather than the page it points at.
        absolute = target if target.is_absolute() else Path.cwd() / target
        deleted = absolute.is_file()
        if deleted:
            absolute.unlink()
            if absolute.parent.name in paths.FIELD_DIRS:
                embedder = embedder or build_embedder()
                fieldindex.forget(absolute.parent, embedder, absolute.name)
        records.append({"path": str(target), "deleted": deleted})
    return records


def delete_command(targets: list[Path] = PATHS, *, as_json: bool = JSON) -> None:
    """Delete files from the vault, dropping a page's index row with it.

    Raises:
        Exit: With code 1 when a path is outside the vault or does not exist.
    """
    vault = require_vault()
    _check_vault_boundary(targets, vault)
    records = _delete_targets(targets)

    missing = [record["path"] for record in records if not record["deleted"]]
    if as_json:
        emit_json(records)
    else:
        for record in records:
            if record["deleted"]:
                pp.success(f"deleted {record['path']}")
    if missing:
        if as_json:
            raise typer.Exit(1)
        fail(f"not found: {', '.join(str(path) for path in missing)}")
