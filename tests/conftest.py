"""Fixtures shared across the test suite."""

from __future__ import annotations

import pytest

from sessionmemory.lib.bootstrap import initialize


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch) -> None:
    """Keep Rich from wrapping prose a test asserts on.

    `pp` sizes its console from `COLUMNS`, and a temp path pytest builds can be long
    enough to wrap mid-value on a narrow one. A test asserting that a path appears in
    the output then fails on the wrapping rather than on the behavior, and only on the
    machines whose temp paths are long enough, which makes it look like flakiness.
    """
    monkeypatch.setenv("COLUMNS", "1000")


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch) -> None:
    """Select the deterministic embedder for the whole suite.

    The real model costs seconds per load and downloads 520MB on a machine with no cached
    copy, and no test here depends on a real embedding.
    """
    monkeypatch.setenv("SESSIONMEMORY_EMBEDDER", "stub")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Build an initialized vault and point the environment at it."""
    root = tmp_path / "vault"
    initialize(root)
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(root))
    return root
