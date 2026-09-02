#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13"
# dependencies = []
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

The gate's own work is quick; the timeout of 60 covers the vault commit's 35s
worst case with headroom.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

HOOKS_ROOT = Path(__file__).resolve().parent
if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from sessionhooks.config import SessionMemoryConfig  # noqa: E402
from sessionhooks.headless import is_headless  # noqa: E402
from sessionhooks.io import read_payload  # noqa: E402
from sessionhooks.store import Store  # noqa: E402
from sessionhooks.sweep import in_progress, run_sweep  # noqa: E402
from sessionhooks.vaultcli import VaultCLI  # noqa: E402


def main() -> None:
    """Trigger the memory sweep when enabled, then commit the vault regardless."""
    if is_headless():
        return
    payload = read_payload()
    cfg = SessionMemoryConfig.load(project_dir=os.environ.get("CLAUDE_PROJECT_DIR"))
    if cfg.sweep_enabled:
        run_sweep(payload, env=os.environ)
    cwd = Path(payload.get("cwd") or Path.cwd())
    store = Store.for_cwd(cwd=cwd, env=os.environ)
    if in_progress(store, now=time.time()):
        return  # the worker commits its own writes when it finishes
    vault = VaultCLI.discover(env=os.environ, configured=cfg.vault_root)
    if vault is not None:
        vault.commit(env=os.environ)


if __name__ == "__main__":
    with contextlib.suppress(Exception):  # fail-open: a hook never wedges the session
        main()
    sys.exit(0)
