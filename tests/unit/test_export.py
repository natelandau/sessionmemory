"""Test export functionality for writing fields as .memoryfield.zip archives."""

from __future__ import annotations

import zipfile

from sessionmemory.lib import export, field
from sessionmemory.lib.embed import StubEmbedder

NOW = "2026-09-01T14:03:11Z"


def test_export_zips_pages_and_a_fresh_index_flat(tmp_path):
    """Verify the archive holds every page and the model-named index at its root."""
    directory = tmp_path / "learnings"
    field.new_page(directory, title="A", summary="s", body="", now=NOW)

    output = export.export_field(directory, StubEmbedder(), tmp_path / "out.memoryfield.zip")

    with zipfile.ZipFile(output) as archive:
        assert sorted(archive.namelist()) == ["a.md", "stub.sqlite3"]


def test_export_field_omits_the_index_when_the_field_does_not_exist(tmp_path):
    """Verify exporting an absent field writes an empty archive rather than failing.

    `fieldindex.refresh` returns early on a missing directory without creating an index
    file, so the archive holds neither pages nor an index.
    """
    directory = tmp_path / "learnings"

    output = export.export_field(directory, StubEmbedder(), tmp_path / "out.memoryfield.zip")

    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == []
