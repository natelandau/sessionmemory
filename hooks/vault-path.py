#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13"
# dependencies = []
#
# # ty checks a script as its own project, so the repo's extra-paths do not reach it.
# [tool.ty.environment]
# extra-paths = ["."]
# ///

"""Print an absolute path for the current project's memory.

The sole shell-facing facade skills use to locate anything, so no skill ever
re-derives a path in prose. Two sources sit behind it and the caller does not
need to know which is which:

- The vault, for anything durable. Resolved by running the vault CLI, which owns
  where a project's notes live.
- Machine-local state, for the consume-once handoff. It is a baton that lives
  for minutes and never belongs in a synced, committed vault.

    vault-path.py --handoff      # the pending session handoff
    vault-path.py --state-dir    # machine-local state for this project
    vault-path.py --backlog      # the project's backlog.md
    vault-path.py --learnings    # this project's learnings field
    vault-path.py --specs        # this project's specs folder
    vault-path.py --plans        # this project's plans folder
    vault-path.py --logs         # this project's logs field
    vault-path.py --project      # the project's vault folder
    vault-path.py --cli          # the vault CLI itself

Exactly one flag per call. A vault-backed flag exits 2 when no vault is
reachable, because printing nothing would read as "the path is empty" rather
than "there is nowhere to look".
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HOOKS_ROOT = Path(__file__).resolve().parent
if str(HOOKS_ROOT) not in sys.path:
    sys.path.insert(0, str(HOOKS_ROOT))

from sessionhooks.config import SessionMemoryConfig  # noqa: E402
from sessionhooks.store import Store  # noqa: E402
from sessionhooks.vaultcli import VaultCLI  # noqa: E402

EXIT_NO_VAULT = 2

NO_VAULT = "sessionmemory: no reachable vault"

# Flag -> the key in `sessionmemory project --json` output that answers it.
VAULT_KEYS = {
    "project": "project_dir",
    "learnings": "learnings",
    "backlog": "backlog",
    "specs": "specs",
    "plans": "plans",
    "logs": "logs",
}

# Flag -> the Store attribute that answers it.
STORE_ATTRS = {"handoff": "handoff_path", "state_dir": "state_dir"}

# Answered by the vault's location alone, without asking it to resolve a project.
CLI_FLAG = "cli"


def main() -> int:
    """Resolve the requested path for the current directory and print it."""
    parser = argparse.ArgumentParser(description="Resolve a project-memory path.")
    target = parser.add_mutually_exclusive_group(required=True)
    for flag in (*STORE_ATTRS, *VAULT_KEYS, CLI_FLAG):
        target.add_argument(
            f"--{flag.replace('_', '-')}", action="store_const", const=flag, dest="target"
        )
    args = parser.parse_args()

    cwd = Path.cwd()
    if args.target in STORE_ATTRS:
        store = Store.for_cwd(cwd=cwd, env=os.environ)
        print(getattr(store, STORE_ATTRS[args.target]))  # noqa: T201
        return 0

    cfg = SessionMemoryConfig.load(project_dir=os.environ.get("CLAUDE_PROJECT_DIR"))
    vault = VaultCLI.discover(env=os.environ, configured=cfg.vault_root)
    if vault is None:
        print(NO_VAULT, file=sys.stderr)  # noqa: T201
        return EXIT_NO_VAULT
    if args.target == CLI_FLAG:
        print(vault.cli)  # noqa: T201
        return 0

    paths = vault.project_paths(cwd=cwd, env=os.environ) or {}
    # A vault that answers without the key this flag names speaks a different
    # payload than this plugin does; say so rather than printing a stray path.
    resolved = paths.get(VAULT_KEYS[args.target])
    if not resolved:
        print(NO_VAULT, file=sys.stderr)  # noqa: T201
        return EXIT_NO_VAULT
    print(resolved)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
