#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13"
# dependencies = []
#
# # ty checks a script as its own project, so the repo's extra-paths do not reach it.
# [tool.ty.environment]
# extra-paths = ["."]
# ///

"""Finish a manual sweep: run the worker's backstops on what the agent wrote.

The second half of `/sessionmemory:sweep`. Containment, the secret scrub, the shape
checks, and the CLAUDE.local.md guard run on every file that changed since
`sweep-prompt.py` ran. A zero exit inside the skill's turn is what the automatic
sweep reads as a finished manual sweep, so any failure exits non-zero and leaves
the session to the automatic sweep.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOOKS_ROOT = Path(__file__).resolve().parent
if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from sessionhooks.hookmain import begin  # noqa: E402
from sessionhooks.manual import SESSION_ENV, ManualSweep, ManualSweepError  # noqa: E402
from sessionhooks.vaultcli import VaultCLI  # noqa: E402


def main() -> int:
    """Print what changed and each backstop's note, or the reason the check could not run."""
    payload = {"cwd": str(Path.cwd()), "session_id": os.environ.get(SESSION_ENV, "")}
    ctx = begin("manual-sweep", payload=payload, env=os.environ)
    try:
        vault = VaultCLI.locate(env=os.environ, configured=ctx.config.vault_root)
        counts, notes = ManualSweep(
            store=ctx.store, config=ctx.config, vault_root=vault, env=os.environ, cwd=ctx.cwd
        ).finish()
    except ManualSweepError as exc:
        ctx.log.info("manual sweep not checked: %s", exc)
        print(f"sessionmemory: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    except Exception:
        ctx.log.exception("manual sweep check failed")
        print("sessionmemory: the sweep check failed; see the hooks log", file=sys.stderr)  # noqa: T201
        return 1
    print(f"sweep checked: {counts}")  # noqa: T201
    for note in notes:
        print(f"  {note}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
