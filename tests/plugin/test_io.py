"""Tests for the hooks' stdin payload parsing.

A hook is handed its event as JSON on stdin by a harness it does not control, so
every failure here has to read as "nothing to act on" rather than as an exception.
These functions are otherwise exercised only through real subprocesses, where the
assertions are about the hook's exit code rather than about what it parsed.
"""

from __future__ import annotations

import io as _io

import pytest
from sessionhooks.io import (  # ty: ignore[unresolved-import]
    MAX_STDIN_BYTES,
    parse_json_object,
    read_payload,
)


def test_parse_json_object_returns_the_object() -> None:
    """Verify a JSON object survives parsing intact."""
    assert parse_json_object('{"cwd": "/srv/repo", "source": "startup"}') == {
        "cwd": "/srv/repo",
        "source": "startup",
    }


@pytest.mark.parametrize(
    "raw",
    ['["a", "b"]', '"a string"', "42", "null", "true"],
    ids=["array", "string", "number", "null", "bool"],
)
def test_parse_json_object_rejects_a_non_object(raw: str) -> None:
    """Verify valid JSON that is not an object reads as nothing to act on."""
    assert parse_json_object(raw) == {}


@pytest.mark.parametrize("raw", ["", "{", "not json at all", '{"unterminated": '])
def test_parse_json_object_rejects_malformed_input(raw: str) -> None:
    """Verify unparsable input reads as nothing to act on rather than raising."""
    assert parse_json_object(raw) == {}


def test_read_payload_parses_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the event a harness writes to stdin reaches the caller as a mapping."""
    monkeypatch.setattr("sys.stdin", _io.StringIO('{"cwd": "/repo"}'))

    assert read_payload() == {"cwd": "/repo"}


def test_read_payload_rejects_an_oversized_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a runaway or truncated stream is refused rather than parsed into a partial."""
    monkeypatch.setattr("sys.stdin", _io.StringIO("x" * (MAX_STDIN_BYTES + 1)))

    assert read_payload() == {}


def test_read_payload_survives_an_unreadable_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify a stdin that raises leaves the hook with nothing to act on, not an exception."""

    class _Broken:
        def read(self, _size: int) -> str:
            message = "stdin is gone"
            raise OSError(message)

    monkeypatch.setattr("sys.stdin", _Broken())

    assert read_payload() == {}
