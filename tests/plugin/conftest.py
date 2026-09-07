"""Shared fixtures for the hook tests.

The hooks directory is placed on sys.path so tests import the engine directly as
`from sessionhooks.store import Store`. The hooks are standalone scripts run by uv with
no dependencies, so they are not importable as part of the `sessionmemory`
distribution and cannot be reached any other way.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

from sessionhooks.log import LOG_NAME  # noqa: E402  # ty: ignore[unresolved-import]
from sessionhooks.store import PLUGIN_NS  # noqa: E402  # ty: ignore[unresolved-import]


@pytest.fixture(scope="session")
def hooks_dir() -> Path:
    """Resolve the plugin's hooks directory."""
    return _HOOKS


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HOME at a throwaway directory for every plugin test.

    SessionMemoryConfig.load reads ~/.claude/sessionmemory.toml through
    Path.home(), which resolves from the process environment regardless of any
    env mapping a test hands to a hook function directly; a developer's real
    file must never steer a test.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))


def hooks_log_text(state_home: Path) -> str:
    """Everything written to the shared hooks log under `state_home`, or "" if none exists."""
    path = state_home / PLUGIN_NS / LOG_NAME
    return path.read_text(encoding="utf-8") if path.exists() else ""
