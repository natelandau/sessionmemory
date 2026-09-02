"""Bounded, fail-open stdin payload parsing for the hook scripts.

A hook must never crash on malformed input; an unreadable, oversized, or
non-object payload is treated as "nothing to act on" (an empty dict).
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Upper bound on stdin we will parse. Sized far above any real hook payload so
# only a pathological or truncated stream reaches it; oversized input fails open
# to {} rather than being read unbounded into memory.
MAX_STDIN_BYTES = 10 * 1024 * 1024


def read_payload() -> dict[str, Any]:
    """Parse the hook JSON payload from stdin, or return {} on any error.

    Reads at most `MAX_STDIN_BYTES + 1` characters so a truncated or runaway
    stream is rejected outright rather than parsed into a misleading partial.
    """
    try:
        raw = sys.stdin.read(MAX_STDIN_BYTES + 1)
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if len(raw) > MAX_STDIN_BYTES:
        return {}
    return parse_json_object(raw)


def parse_json_object(raw: str) -> dict[str, Any]:
    """Parse a JSON string into a dict, or return {} on any error.

    A non-object JSON value (array, scalar, null) yields {}.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
