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


@pytest.fixture(scope="session")
def hooks_dir() -> Path:
    """Resolve the plugin's hooks directory."""
    return _HOOKS
