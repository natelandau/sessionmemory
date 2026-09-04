#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13"
# dependencies = []
#
# # ty checks a script as its own project, so the repo's extra-paths do not reach it.
# [tool.ty.environment]
# extra-paths = ["."]
# ///

"""SessionStart hook: commit the vault, inject this project's memory, and consume any pending handoff.

Commits whatever the last session's sweep and any hand edits left behind, so the
vault's history is never more than one session behind its files, in every case
`SessionEnd` does; skipped only while a sweep worker holds a fresh lock, since it
commits its own writes when it finishes. Then asks the vault CLI to render this
project's memory block and returns it as non-blocking `additionalContext`. It
also records two things the end-of-session sweep cannot recover later: the
transcript path, so the sweep finds it after a `/clear`, and the commit this
repository was on, so the session log can report the whole span rather than only
what happened after the first fire. Both belong to the sweep, so they follow the
sweep toggle rather than the inject one, and the commit is not re-recorded on a
start that continues a session already on record.

Independently, on any start other than `resume` it injects the consume-once
`HANDOFF.md` the user wrote, ahead of the memory block, and deletes it only
after the inject is emitted. The handoff is an explicit user artifact, so it is
carried even when memory injection is disabled.

When the project is not registered with the vault and the directory is a git
working tree, it registers the repository through the CLI before injecting, and
says which slug it was filed under. A directory outside git is never registered
on its own, since a slug is permanent and a session opened in a home or scratch
directory would leave a project named after it behind; such a directory, and a
registration the CLI refuses, are told the command that registers by hand.

The schema version is deliberately not checked here. This path only reads, and a
block rendered by a vault one version ahead is still readable text; it is the
writing paths that must refuse a schema they do not speak.

No-ops when running inside the headless sweep agent, and when no vault is
reachable. Fail-open: any error exits 0 rather than wedging session start.

The hook's timeout budgets its worst case: `Store.for_cwd` 5s, `head_commit` 5s,
two `VaultCLI.discover` handshakes 10s, the vault commit 35s, `VaultCLI.resolve` 5s,
`VaultCLI.register` 5s, `VaultCLI.inject` 25s, 90s in all; the timeout is 100 to
stay ahead of that sum. Raising any of those numbers means raising it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HOOKS_ROOT = Path(__file__).resolve().parent
if str(HOOKS_ROOT) not in sys.path:  # pragma: no cover - conftest.py always adds it first
    sys.path.insert(0, str(HOOKS_ROOT))

from sessionhooks.config import SessionMemoryConfig  # noqa: E402
from sessionhooks.headless import is_headless  # noqa: E402
from sessionhooks.io import read_payload  # noqa: E402
from sessionhooks.store import Store, head_commit  # noqa: E402
from sessionhooks.sweep import in_progress  # noqa: E402
from sessionhooks.vaultcli import VaultCLI  # noqa: E402

# Start sources that carry on a session already recorded rather than opening a new
# one. Re-recording the base commit for either would move it forward past work the
# session's log has already reported, cutting that log's span down to the tail.
CONTINUATION_SOURCES = frozenset({"resume", "compact"})


# The CLI's own guidance block names the commands a session needs. Naming the skill
# that carries the rest is this layer's job: a CLI installed without this plugin has
# no such skill, so a reference from there would point at nothing.
SKILL_POINTER = "The `cli` skill carries the full command surface for this vault."


def _unregistered_hint(vault: VaultCLI) -> str:
    """The line a session in an unregistered project receives instead of memory."""
    return (
        "This project is not registered with the vault, so it has no memory yet. "
        f"Register it with: {vault.command} project --register"
    )


def _registered_note(slug: str) -> str:
    """The line a session receives when this start registered its repository."""
    return f"This repository was registered with the vault as project '{slug}'."


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of `data` to `fd`, looping past short writes.

    A single os.write may accept fewer bytes than given (e.g. a nearly-full pipe
    buffer) without raising, so one call cannot be trusted to have emitted the
    whole payload; a partial write would otherwise truncate the JSON.
    """
    while data:
        written = os.write(fd, data)
        data = data[written:]


def _record_session_state(store: Store, *, payload: dict[str, Any], cwd: Path) -> None:
    """Save what the sweep will need and cannot recover once the session is over."""
    store.save_transcript_pointer(payload.get("transcript_path") or "")
    if payload.get("source") not in CONTINUATION_SOURCES:
        store.save_base_commit(head_commit(cwd=cwd, env=os.environ))


def _memory_block(cfg: SessionMemoryConfig, *, cwd: Path) -> str | None:
    """The project's memory block, the unregistered hint, or None when inject is off."""
    if not cfg.inject_enabled:
        return None
    vault = VaultCLI.discover(env=os.environ, configured=cfg.vault_root)
    if vault is None:
        return None
    resolution = vault.resolve(cwd=cwd, env=os.environ)
    registered = resolution is not None and resolution.get("registered") is True
    slug: str | None = None
    if not registered:
        # A slug is permanent, so only a git working tree is registered unattended:
        # a session opened in a home or scratch directory must not file a project
        # named after it. Every other refusal belongs to the CLI.
        if resolution is None or not resolution.get("repo_root"):
            return _unregistered_hint(vault)
        slug = vault.register(cwd=cwd, env=os.environ)
        if slug is None:
            return _unregistered_hint(vault)
    blocks: list[str] = []
    if slug is not None:
        blocks.append(_registered_note(slug))
    memory = vault.inject(cwd=cwd, env=os.environ)
    if memory:
        blocks.append(f"{memory}\n\n{SKILL_POINTER}")
    return "\n\n".join(blocks) or None


def main() -> None:  # pragma: no cover - runs only as a subprocess, where coverage cannot see it
    """Inject the handoff (if any) and memory for the current project, unless headless."""
    if is_headless():
        return
    payload = read_payload()
    cfg = SessionMemoryConfig.load(project_dir=os.environ.get("CLAUDE_PROJECT_DIR"))

    cwd = Path(payload.get("cwd") or Path.cwd())
    store = Store.for_cwd(cwd=cwd, env=os.environ)

    # Commit whatever the last session's sweep and any hand edits left behind, so
    # the vault's history is never more than one session behind its files, in
    # every case SessionEnd does. Skipped while a sweep worker holds a fresh
    # lock: it commits its own writes when it finishes.
    vault = VaultCLI.discover(env=os.environ, configured=cfg.vault_root)
    if vault is not None and not in_progress(store, now=time.time()):
        vault.commit(env=os.environ)

    # Consume the handoff on any start except `resume` (which may be the same session
    # that wrote it). A denylist, not an allowlist of known sources, keeps this working
    # if upstream adds a start source, and consumes rather than stranding the baton on
    # an unknown or missing source.
    source = payload.get("source")
    consume_handoff = source != "resume"
    if not (consume_handoff or cfg.inject_enabled or cfg.sweep_enabled):
        return  # resume with everything off: nothing left to do

    blocks: list[str] = []

    # Handoff first (freshest, most task-specific) and independent of inject config,
    # since the user explicitly created it. Read now, delete only after a clean emit.
    handoff_text = store.read_handoff() if consume_handoff else None
    if handoff_text:
        blocks.append(handoff_text)

    # Tied to the sweep toggle, not the inject one: a user who turns injection off
    # still gets a sweep, and it is the sweep that reads this state.
    if cfg.sweep_enabled:
        _record_session_state(store, payload=payload, cwd=cwd)

    memory = _memory_block(cfg, cwd=cwd)
    if memory:
        blocks.append(memory)

    if not blocks:
        return

    rendered = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n\n".join(blocks),
            }
        }
    )
    # Write unbuffered so a broken stdout fails synchronously here, rather than
    # leaving buffered output for interpreter shutdown to choke on (which would
    # override the fail-open exit 0). The baton is retired only after the whole
    # payload is confirmed written, so a failed or partial emit never loses it.
    try:
        _write_all(sys.stdout.fileno(), (rendered + "\n").encode("utf-8"))
    except OSError:
        return
    if handoff_text:
        store.delete_handoff()


if __name__ == "__main__":  # pragma: no cover - runs only as a subprocess
    with contextlib.suppress(Exception):  # fail-open: a hook never wedges the session
        main()
    sys.exit(0)
