"""Write one field as a `.memoryfield.zip`, the spec's archival transport."""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

from sessionmemory.lib import field, fieldindex

if TYPE_CHECKING:
    from pathlib import Path

    from sessionmemory.lib.embed import Embedder


def export_field(directory: Path, embedder: Embedder, output: Path) -> int:
    """Refresh the index, then archive every page and the index flat at the zip root.

    Returns:
        int: How many pages the archive holds.
    """
    fieldindex.refresh(directory, embedder)
    pages = field.iter_pages(directory)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in pages:
            archive.write(path, arcname=path.name)
        index = fieldindex.index_path(directory, embedder)
        if index.is_file():
            archive.write(index, arcname=index.name)
    return len(pages)
