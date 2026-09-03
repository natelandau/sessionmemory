"""Fixtures shared across the test suite."""

from __future__ import annotations

import os

import pytest

from sessionmemory.lib import config
from sessionmemory.lib.bootstrap import initialize


@pytest.fixture(autouse=True)
def _stub_embedder(monkeypatch) -> None:
    """Select the deterministic embedder for the whole suite.

    The real model costs seconds per load and downloads 520MB on a machine with no cached
    copy, and no test here depends on a real embedding.
    """
    monkeypatch.setenv("SESSIONMEMORY_EMBEDDER", "stub")


@pytest.fixture(autouse=True)
def _isolated_config_file(tmp_path, monkeypatch) -> None:
    """Point the CLI's config file at a path that does not exist.

    `vault_root` falls back to `~/.claude/sessionmemory.toml` when the environment is
    unset, so a test of the unset case would otherwise read the developer's real file.
    """
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config" / "sessionmemory.toml")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Build an initialized vault and point the environment at it."""
    root = tmp_path / "vault"
    initialize(root)
    monkeypatch.setenv("SESSIONMEMORY_VAULT", str(root))
    return root


@pytest.fixture(autouse=True)
def _git_identity(tmp_path_factory, monkeypatch) -> None:
    """Give git a committer identity that does not depend on the machine.

    Fixtures commit into throwaway repositories, and git refuses a commit with no
    identity on a host where it cannot derive one from the login and hostname, which a
    CI runner is. Pointing `GIT_CONFIG_GLOBAL` at a file of our own also keeps a
    developer's real global config, such as commit signing, out of the suite. The hooks
    strip `GIT_`-prefixed variables before running git, so their tests set a repo-local
    identity themselves.

    Every `GIT_`-prefixed variable is dropped first. Git exports `GIT_INDEX_FILE` and
    its siblings into a pre-commit hook, and a suite run from one would otherwise point
    every throwaway repository's git at the index of the repository being committed.
    """
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    config = tmp_path_factory.mktemp("git") / "config"
    config.write_text('[user]\n\tname = "Test"\n\temail = "test@example.com"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
