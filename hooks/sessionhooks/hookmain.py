"""The shared opening sequence every hook script runs before its own body.

`SessionStart`, `SessionEnd`, and `PreCompact` all load the config, resolve the
project's `Store`, and configure the shared log the same way, in the same order,
before anything that can fail. Configuring the log first is the point: once
`begin` returns, every later exception in the hook's body is caught by a logger
that already knows the step, the project, and the session, so a crash is on
record rather than silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sessionhooks.config import SessionMemoryConfig
from sessionhooks.log import configure
from sessionhooks.store import Store

if TYPE_CHECKING:
    import logging
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class HookContext:
    """Everything a hook's body needs, resolved once by `begin`."""

    payload: dict[str, Any]
    config: SessionMemoryConfig
    cwd: Path
    store: Store
    log: logging.Logger


def begin(step: str, *, payload: dict[str, Any], env: Mapping[str, str]) -> HookContext:
    """Load the config, resolve the store, and configure the log for one hook run.

    `payload` is read by the caller rather than here, so a test can replace a hook
    module's own `read_payload` and still have `begin` see the substitute.
    """
    config = SessionMemoryConfig.load(project_dir=env.get("CLAUDE_PROJECT_DIR"))
    cwd = Path(payload.get("cwd") or Path.cwd())
    store = Store.for_cwd(cwd=cwd, env=env)
    log = configure(
        step,
        config=config,
        env=env,
        project=store.name,
        session_id=str(payload.get("session_id") or ""),
    )
    return HookContext(payload=payload, config=config, cwd=cwd, store=store, log=log)
