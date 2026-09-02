"""Tests for helpers shared by every command module."""

from __future__ import annotations

import pytest
import typer

from sessionmemory.commands import _common


def test_build_embedder_falls_back_to_the_real_embedder(monkeypatch, mocker):
    """Verify build_embedder defers to the real model when the stub is not selected."""
    monkeypatch.delenv(_common.EMBEDDER_ENV_VAR, raising=False)
    sentinel = object()
    default_embedder = mocker.patch(
        "sessionmemory.commands._common.embed.default_embedder",
        return_value=sentinel,
        autospec=True,
    )

    assert _common.build_embedder() is sentinel
    default_embedder.assert_called_once_with()


def test_resolve_body_refuses_both_body_and_body_file(tmp_path, capsys):
    """Verify --body and --body-file cannot both carry a value."""
    body_file = tmp_path / "b.md"
    body_file.write_text("from file", encoding="utf-8")

    with pytest.raises(typer.Exit) as excinfo:
        _common.resolve_body("from --body", body_file)

    assert excinfo.value.exit_code == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_resolve_body_reports_an_unreadable_file(tmp_path, capsys):
    """Verify a --body-file that cannot be read as text fails with the OSError reason."""
    directory = tmp_path / "not-a-file"
    directory.mkdir()

    with pytest.raises(typer.Exit) as excinfo:
        _common.resolve_body("", directory)

    assert excinfo.value.exit_code == 1
    assert "cannot read" in capsys.readouterr().err


def test_resolve_body_reports_invalid_utf8(tmp_path, capsys):
    """Verify a --body-file holding bytes that are not valid UTF-8 is refused."""
    body_file = tmp_path / "b.md"
    body_file.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(typer.Exit) as excinfo:
        _common.resolve_body("", body_file)

    assert excinfo.value.exit_code == 1
    assert "is not valid UTF-8 text" in capsys.readouterr().err
