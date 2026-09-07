"""Verify the hooks' shared log: where it goes, what a line looks like, and that it never raises."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sessionhooks import log as log_mod  # ty: ignore[unresolved-import]
from sessionhooks.config import SessionMemoryConfig  # ty: ignore[unresolved-import]

from tests.plugin.conftest import hooks_log_text

if TYPE_CHECKING:
    import logging
    from pathlib import Path

    import pytest

LINE = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (?P<level>\S+)\s+\[(?P<step>[a-z-]+)\s*\] "
    r"(?P<project>\S+): (?P<message>.*) \(session=(?P<session>[^)]+)\)$"
)


def _configure(tmp_path: Path, **overrides: object) -> logging.Logger:
    cfg = SessionMemoryConfig(**overrides)
    return log_mod.configure(
        "sessionend",
        config=cfg,
        env={"XDG_STATE_HOME": str(tmp_path)},
        project="invoice-api",
        session_id="05ecdee7-10e1-4358-a3b7-d44ee6cf3f64",
    )


def _lines(tmp_path: Path) -> list[str]:
    return hooks_log_text(tmp_path).splitlines()


def test_default_path_sits_under_the_xdg_state_root(tmp_path: Path) -> None:
    """Verify the log lands beside the per-project stores when no path is configured."""
    path = log_mod.log_path(SessionMemoryConfig(), {"XDG_STATE_HOME": str(tmp_path)})
    assert path == tmp_path / "sessionmemory" / "hooks.log"


def test_configured_path_wins_and_expands_a_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify log.path is honored and `~` means the user's home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    path = log_mod.log_path(SessionMemoryConfig(log_path="~/logs/hooks.log"), {})
    assert path == tmp_path / "logs" / "hooks.log"


def test_a_line_carries_the_bound_context_in_the_documented_shape(tmp_path: Path) -> None:
    """Verify one INFO line reads as the spec shows: time, level, [step], project, message, session."""
    logger = _configure(tmp_path)
    logger.info("sweep skipped: below threshold (user messages 2/3)")
    (line,) = _lines(tmp_path)
    match = LINE.match(line)
    assert match is not None, line
    assert match["level"] == "INFO"
    assert match["step"] == "sessionend"
    assert match["project"] == "invoice-api"
    assert match["message"] == "sweep skipped: below threshold (user messages 2/3)"
    assert match["session"] == "05ecdee7"


def test_the_level_and_step_columns_are_padded_to_fixed_widths(tmp_path: Path) -> None:
    """Verify INFO and ERROR lines align, and every step occupies twelve characters inside its brackets."""
    logger = _configure(tmp_path)
    logger.info("one")
    logger.error("two")
    first, second = _lines(tmp_path)
    assert first.index("[sessionend") == second.index("[sessionend")
    assert first.index("] invoice-api:") == second.index("] invoice-api:")
    assert "INFO  [sessionend  ] invoice-api: one (session=05ecdee7)" in first
    assert "ERROR [sessionend  ] invoice-api: two (session=05ecdee7)" in second


def test_a_module_logger_inherits_the_bound_context(tmp_path: Path) -> None:
    """Verify a module logging under the root needs no logger handed to it."""
    _configure(tmp_path)
    log_mod.logger("commit").info("committed abc1234")
    (line,) = _lines(tmp_path)
    assert "[sessionend  ] invoice-api: committed abc1234 (session=05ecdee7)" in line


def test_bind_changes_the_step_column_for_later_lines(tmp_path: Path) -> None:
    """Verify the detached worker can relabel itself as `sweep` after the hook configured the log."""
    logger = _configure(tmp_path)
    log_mod.bind(step="sweep")
    logger.info("sweep finished")
    (line,) = _lines(tmp_path)
    assert "[sweep       ] invoice-api: sweep finished (session=05ecdee7)" in line


def test_an_empty_session_id_is_written_as_a_dash(tmp_path: Path) -> None:
    """Verify a payload with no session id still yields a parseable line."""
    cfg = SessionMemoryConfig()
    logger = log_mod.configure(
        "precompact", config=cfg, env={"XDG_STATE_HOME": str(tmp_path)}, project="", session_id=""
    )
    logger.info("hello")
    (line,) = _lines(tmp_path)
    assert "[precompact  ] -: hello (session=-)" in line


def test_configuring_twice_does_not_duplicate_lines(tmp_path: Path) -> None:
    """Verify a second configure in one process replaces the handler rather than adding one."""
    _configure(tmp_path)
    logger = _configure(tmp_path)
    logger.info("once")
    assert len(_lines(tmp_path)) == 1


def test_level_off_writes_nothing_and_creates_no_file(tmp_path: Path) -> None:
    """Verify `off` is silent."""
    logger = _configure(tmp_path, log_level="off")
    logger.error("nothing")
    assert not (tmp_path / "sessionmemory" / log_mod.LOG_NAME).exists()


def test_level_filters_below_the_configured_threshold(tmp_path: Path) -> None:
    """Verify DEBUG detail stays out at the default level and appears at debug."""
    logger = _configure(tmp_path)
    logger.debug("hidden")
    logger.info("shown")
    assert [LINE.match(x)["message"] for x in _lines(tmp_path)] == ["shown"]
    logger = _configure(tmp_path, log_level="debug")
    logger.debug("now visible")
    assert LINE.match(_lines(tmp_path)[-1])["message"] == "now visible"


def _raise_boom() -> None:
    msg = "boom"
    raise ValueError(msg)


def test_an_exception_line_carries_its_traceback(tmp_path: Path) -> None:
    """Verify a swallowed exception is diagnosable from the log alone."""
    logger = _configure(tmp_path)
    try:
        _raise_boom()
    except ValueError:
        logger.exception("hook failed")
    text = "\n".join(_lines(tmp_path))
    assert "ERROR [sessionend  ] invoice-api: hook failed (session=05ecdee7)" in text
    assert "Traceback" in text
    assert "ValueError: boom" in text


def test_rotation_keeps_the_configured_number_of_backups(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify the log rolls over at the size cap and keeps only BACKUP_COUNT old files."""
    monkeypatch.setattr(log_mod, "MAX_BYTES", 300)
    logger = _configure(tmp_path)
    for i in range(40):
        logger.info("line %d %s", i, "x" * 60)
    directory = tmp_path / "sessionmemory"
    names = sorted(p.name for p in directory.iterdir())
    assert names == ["hooks.log", "hooks.log.1", "hooks.log.2"]


def test_an_unwritable_path_never_raises(tmp_path: Path) -> None:
    """Verify a path whose parent is a plain file leaves the hook silent, not crashed."""
    blocker = tmp_path / "sessionmemory"
    blocker.write_text("in the way", encoding="utf-8")
    logger = _configure(tmp_path)
    logger.info("dropped")  # must not raise
    assert blocker.read_text(encoding="utf-8") == "in the way"


def test_a_path_that_cannot_be_expanded_never_raises(tmp_path: Path) -> None:
    """Verify a log.path tilde expansion cannot resolve leaves the hook silent, not crashed."""
    logger = _configure(tmp_path, log_path="~nosuchuser_xyz/hooks.log")
    logger.info("dropped")  # must not raise
    assert not (tmp_path / "sessionmemory").exists()


def test_an_unrecognized_level_string_is_treated_as_off(tmp_path: Path) -> None:
    """Verify the module never guesses: a level the config layer did not normalize is silent."""
    logger = _configure(tmp_path, log_level="verbose")
    logger.error("nothing")
    assert not (tmp_path / "sessionmemory" / log_mod.LOG_NAME).exists()
