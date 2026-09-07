"""Verify hookmain.begin: the shared opening sequence every hook script runs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sessionhooks import hookmain  # ty: ignore[unresolved-import]

from tests.plugin.conftest import hooks_log_text

if TYPE_CHECKING:
    import pytest


def test_begin_resolves_the_store_and_configures_the_log(tmp_path: Path) -> None:
    """Verify the returned context names the project and its log writes under XDG_STATE_HOME."""
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "CLAUDE_PROJECT_DIR": str(proj),
    }
    ctx = hookmain.begin("sessionstart", payload={"cwd": str(proj), "session_id": "abc"}, env=env)
    assert ctx.store.name == "proj"
    ctx.log.info("hello")
    assert "hello" in hooks_log_text(tmp_path / "state")


def test_begin_falls_back_when_the_payload_has_no_cwd_or_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify a payload missing cwd and session_id still yields a usable context."""
    monkeypatch.chdir(tmp_path)
    env = {"XDG_STATE_HOME": str(tmp_path / "state")}
    ctx = hookmain.begin("precompact", payload={}, env=env)
    assert ctx.cwd == Path.cwd()
    ctx.log.info("hello")
    assert "session=-" in hooks_log_text(tmp_path / "state")
