#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13"
# dependencies = []
#
# # ty checks a script as its own project, so the repo's extra-paths do not reach it.
# [tool.ty.environment]
# extra-paths = ["."]
# ///

"""SessionEnd hook: trigger the end-of-session memory sweep and commit the vault.

Gating (lock, threshold, transcript window) runs inline; the heavy `claude -p`
pass runs in a detached worker that outlives session teardown. The sweep worker
commits its own writes when it finishes; this hook commits what the vault
already holds, so a session that ends without triggering a sweep still lands
any outstanding changes. The commit runs even when the sweep is disabled or
skipped by its own gate; only the headless guard and a fresh sweep-worker lock
skip it too, since a hook committing while a worker may still be writing would
race it. Fail-open: any error exits 0 rather than wedging session end.

Claude Code gives every SessionEnd hook one shared budget, 1.5s by default, and a
plugin's own `timeout` cannot raise it; only the user's
`CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` can. So nothing here starts the vault
CLI: the commit needs only git, and the sweep worker chooses its CLI after it
detaches. A commit cut short is not lost, since the next SessionStart commits it.

Every decision the hook makes is one line in the shared hooks log, and a crash is
logged before the fail-open exit.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

HOOKS_ROOT = Path(__file__).resolve().parent
if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from sessionhooks import log  # noqa: E402
from sessionhooks.commit import commit_vault  # noqa: E402
from sessionhooks.headless import is_headless  # noqa: E402
from sessionhooks.hookmain import begin  # noqa: E402
from sessionhooks.io import read_payload  # noqa: E402
from sessionhooks.sweep import in_progress, run_sweep  # noqa: E402
from sessionhooks.vaultcli import COMMIT_GIT_TIMEOUT, VaultCLI  # noqa: E402

if TYPE_CHECKING:
    import logging

    from sessionhooks.config import SessionMemoryConfig
    from sessionhooks.store import Store


def _run(payload: dict, cfg: SessionMemoryConfig, *, store: Store, log: logging.Logger) -> None:
    """Trigger the sweep when enabled, then commit unless a worker owns the vault."""
    if cfg.sweep_enabled:
        run_sweep(payload, env=os.environ, store=store, config=cfg)
    else:
        log.info("sweep skipped: disabled")
    if in_progress(store, now=time.time()):
        log.info("commit skipped: sweep in progress")
        return  # the worker commits its own writes when it finishes
    root = VaultCLI.locate(env=os.environ, configured=cfg.vault_root)
    if root is None:
        log.info("commit skipped: no vault")
        return
    log.info(commit_vault(root, env=os.environ, timeout=COMMIT_GIT_TIMEOUT).describe())


def main() -> None:
    """Configure the log, then run the hook body with its crash on record."""
    if is_headless():
        return
    payload = read_payload()
    # Bound ahead of the try: a crash before begin() configures the log still has
    # something to report to, the import-time NullHandler dropping it quietly.
    logger = log.logger()
    try:
        ctx = begin("sessionend", payload=payload, env=os.environ)
        _run(ctx.payload, ctx.config, store=ctx.store, log=ctx.log)
    except Exception:
        logger.exception("hook failed")


if __name__ == "__main__":
    with contextlib.suppress(Exception):  # fail-open: a hook never wedges the session
        main()
    sys.exit(0)
