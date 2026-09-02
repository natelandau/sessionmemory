"""Write one field as a `.memoryfield.zip`, the spec's archival transport."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

from sessionmemory.lib import field, fieldindex

if TYPE_CHECKING:
    from pathlib import Path

    from sessionmemory.lib.embed import Embedder


def export_field(directory: Path, embedder: Embedder, output: Path) -> Path:
    """Refresh the index, then archive every page and the index flat at the zip root."""
    fieldindex.refresh(directory, embedder)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in field.iter_pages(directory):
            archive.write(path, arcname=path.name)
        index = fieldindex.index_path(directory, embedder)
        if index.is_file():
            archive.write(index, arcname=index.name)
    return output
