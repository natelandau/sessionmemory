"""Recursion guard for the headless sweep agent.

The sweep spawns `claude -p` with SESSIONMEMORY_HEADLESS=1 in its environment. That
child is itself a Claude Code session, so when it ends it fires SessionEnd/Stop
and would re-trigger the very hooks that spawned it. Every hook script
checks this first and no-ops, breaking the loop.
"""

from __future__ import annotations

import os

HEADLESS_ENV = "SESSIONMEMORY_HEADLESS"


def is_headless() -> bool:
    """Return whether this process is running inside the headless sweep agent."""
    return os.environ.get(HEADLESS_ENV) == "1"
