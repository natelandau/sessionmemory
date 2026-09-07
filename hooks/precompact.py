#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13"
# dependencies = []
#
# # ty checks a script as its own project, so the repo's extra-paths do not reach it.
# [tool.ty.environment]
# extra-paths = ["."]
# ///

"""PreCompact hook: trigger the memory sweep before compaction discards context.

Gating (lock, threshold, transcript window) runs inline; the heavy `claude -p`
pass runs in a detached worker that outlives compaction. No-ops when running
inside the headless sweep agent or when the sweep is disabled. Fail-open: any
error exits 0 rather than blocking compaction.

Every decision the hook makes is one line in the shared hooks log, and a crash is
logged before the fail-open exit.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

HOOKS_ROOT = Path(__file__).resolve().parent
if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from sessionhooks import log  # noqa: E402
from sessionhooks.headless import is_headless  # noqa: E402
from sessionhooks.hookmain import begin  # noqa: E402
from sessionhooks.io import read_payload  # noqa: E402
from sessionhooks.sweep import run_sweep  # noqa: E402

if TYPE_CHECKING:
    import logging

    from sessionhooks.config import SessionMemoryConfig
    from sessionhooks.store import Store


def _run(payload: dict, cfg: SessionMemoryConfig, *, store: Store, log: logging.Logger) -> None:
    """Trigger the sweep unless disabled."""
    if not cfg.sweep_enabled:
        log.info("sweep skipped: disabled")
        return
    run_sweep(payload, env=os.environ, store=store, config=cfg)


def main() -> None:
    """Configure the log, then run the hook body with its crash on record."""
    if is_headless():
        return
    payload = read_payload()
    # Bound ahead of the try: a crash before begin() configures the log still has
    # something to report to, the import-time NullHandler dropping it quietly.
    logger = log.logger()
    try:
        ctx = begin("precompact", payload=payload, env=os.environ)
        _run(ctx.payload, ctx.config, store=ctx.store, log=ctx.log)
    except Exception:
        logger.exception("hook failed")


if __name__ == "__main__":
    with contextlib.suppress(Exception):  # fail-open: a hook never wedges the session
        main()
    sys.exit(0)
