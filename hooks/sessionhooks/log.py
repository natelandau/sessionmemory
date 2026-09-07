"""One log for every hook on this machine, and the one place it is configured.

Every hook fails open, so without this file a skipped sweep, a held lock, a start
that found no vault, and a swallowed exception all look the same: nothing. The log
is one file rather than one per project because the question it answers is "what
happened in the last few minutes", and that spans projects. It sits under the XDG
state root beside the per-project stores, rotates by size with fixed constants,
and is written by stdlib `logging` because a hook declares no dependencies.

A line is `DATE TIME LEVEL [step] project: message (session=<id>)`. Only the two
columns with a known width are padded, the level and the step inside its brackets;
a project name has none, so the line's fixed part ends at the colon and the session
id closes the line, labeled so it cannot be read as a commit hash.

`configure` binds the step, project, and session for the whole process, and a
filter stamps them onto every record, so a module logs through `logger("name")`
without being handed anything. The detached sweep worker inherits that
configuration across the fork and relabels itself with `bind(step="sweep")`.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import TYPE_CHECKING

from sessionhooks.store import state_root

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sessionhooks.config import SessionMemoryConfig

LOG_NAME = "hooks.log"
# About 5,000 lines per file at roughly 200 bytes a line, so three files cover weeks
# of ordinary use while never asking anyone to think about disk.
MAX_BYTES = 1_000_000
BACKUP_COUNT = 2
# Enough of a session id to line up a start, its end, and its worker run.
SESSION_CHARS = 8
# The longest step name is `sessionstart`.
STEP_WIDTH = 12
ROOT_LOGGER = "sessionmemory"

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_FORMAT = (
    f"%(asctime)s %(levelname)-5s [%(step)-{STEP_WIDTH}s] "
    "%(project)s: %(message)s (session=%(session)s)"
)
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_context: dict[str, str] = {"step": "-", "project": "-", "session": "-"}

# A record reaching the root logger before `configure` runs (or when it never runs,
# e.g. `begin` raising ahead of it) must not fall through to logging's own
# last-resort stderr handler: a hook shares stderr with the session, and nothing
# may reach it from here.
logging.getLogger(ROOT_LOGGER).addHandler(logging.NullHandler())
logging.getLogger(ROOT_LOGGER).propagate = False


class _ContextFilter(logging.Filter):
    """Stamp the bound step, project, and session onto every record the handler sees."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _context.items():
            setattr(record, key, value)
        return True


def log_path(config: SessionMemoryConfig, env: Mapping[str, str]) -> Path:
    """Where the hooks log: the configured path, else `hooks.log` under the state root."""
    if config.log_path:
        return Path(config.log_path).expanduser()
    return state_root(env) / LOG_NAME


def logger(name: str = "") -> logging.Logger:
    """The root hooks logger, or a module logger beneath it."""
    return logging.getLogger(f"{ROOT_LOGGER}.{name}" if name else ROOT_LOGGER)


def bind(**fields: str) -> None:
    """Rebind any of `step`, `project`, or `session` for every later line in this process."""
    for key, value in fields.items():
        stamped = value[:SESSION_CHARS] if key == "session" else value
        _context[key] = stamped or "-"


def configure(
    step: str,
    *,
    config: SessionMemoryConfig,
    env: Mapping[str, str],
    project: str,
    session_id: str,
) -> logging.Logger:
    """Point the hooks logger at the log file for this process; never raises.

    Replaces any handler a previous call installed, so a process that configures
    twice writes each line once. A level the config layer did not normalize, `off`
    included, leaves a null handler so nothing is written and nothing is created.
    A path that cannot be opened does the same: a hook must never wedge a session
    over its own diagnostics.
    """
    root = logger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.propagate = False
    bind(step=step, project=project, session=session_id)
    # Handler errors would otherwise print to stderr, which a hook shares with the
    # session; the log is diagnostics, not something the session should hear about.
    logging.raiseExceptions = False

    level = LEVELS.get(config.log_level)
    if level is None:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.CRITICAL + 1)
        return root

    try:
        path = log_path(config, env)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
    # Wider than OSError: tilde expansion and home resolution raise RuntimeError,
    # and a value read from config that fails validation raises ValueError.
    except (OSError, RuntimeError, ValueError):
        file_handler = logging.NullHandler()
    file_handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    file_handler.addFilter(_ContextFilter())
    root.addHandler(file_handler)
    root.setLevel(level)
    return root
