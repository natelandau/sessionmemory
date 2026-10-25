#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13"
# dependencies = []
#
# # ty checks a script as its own project, so the repo's extra-paths do not reach it.
# [tool.ty.environment]
# extra-paths = ["."]
# ///

"""Start a manual sweep: print this session's sweep prompt and record the project folder.

The first half of `/sessionmemory:sweep`; `sweep-check.py` is the second. Unlike a
hook it does not fail open: a non-zero exit with the reason on stderr tells the
skill there is nothing to sweep into.
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
    """Print the prompt, or the reason there is none."""
    payload = {"cwd": str(Path.cwd()), "session_id": os.environ.get(SESSION_ENV, "")}
    ctx = begin("manual-sweep", payload=payload, env=os.environ)
    try:
        vault = VaultCLI.discover(env=os.environ, configured=ctx.config.vault_root)
        prompt = ManualSweep(
            store=ctx.store, config=ctx.config, vault=vault, env=os.environ, cwd=ctx.cwd
        ).begin()
    except ManualSweepError as exc:
        ctx.log.info("manual sweep not started: %s", exc)
        print(f"sessionmemory: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    except Exception:
        ctx.log.exception("manual sweep failed to start")
        print("sessionmemory: the sweep failed to start; see the hooks log", file=sys.stderr)  # noqa: T201
        return 1
    print(prompt)  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
