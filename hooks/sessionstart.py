#!/usr/bin/env -S uv run --script

# /// script
# requires-python = ">=3.13"
# dependencies = []
#
# # ty checks a script as its own project, so the repo's extra-paths do not reach it.
# [tool.ty.environment]
# extra-paths = ["."]
# ///

"""SessionStart hook: commit the vault, inject memory, link local entries, consume a handoff.

Commits whatever the last session's sweep and any hand edits left behind, so the
vault's history is never more than one session behind its files, in every case
`SessionEnd` does; skipped only while any project's sweep is running, since the
commit stages the whole vault and the worker commits when it finishes. Then asks the vault
CLI to render this project's memory block and returns it as non-blocking `additionalContext`. It
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

After the memory block, and independently of `[inject] enabled`, it links every local entry
the vault holds for this project (`CLAUDE.local.md`, each skill, each rule) into this
checkout when `[local] link` is on. Claude Code reads them before the hook runs, so a link
made at this start is not loaded. The hook injects `CLAUDE.local.md`'s content itself, once,
on the start that created its link; a newly linked skill or rule is only named, since its
body would cost context the session may never use.

No-ops when running inside the headless sweep agent. When no vault is reachable it
injects a hint saying so, since every hook fails open and the session would otherwise
learn nothing. Fail-open: any error exits 0 rather than wedging session start.

The hook's timeout budgets its worst case: `Store.for_cwd` 5s, `head_commit` 5s, two
`VaultCLI.discover` handshakes 10s, the vault commit 45s (nine git calls at 5s),
`VaultCLI.resolve` 5s, `VaultCLI.register` 5s, `VaultCLI.inject` 25s, `VaultCLI.sync_local`
5s, 105s in all; the timeout is 110 to stay ahead of that sum. Raising any of those numbers
means raising it.

Every decision the hook makes is one line in the shared hooks log, and a crash is
logged before the fail-open exit.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

HOOKS_ROOT = Path(__file__).resolve().parent
if str(HOOKS_ROOT) not in sys.path:  # pragma: no cover - conftest.py always adds it first
    sys.path.insert(0, str(HOOKS_ROOT))

from sessionhooks import log, shape  # noqa: E402
from sessionhooks.headless import is_headless  # noqa: E402
from sessionhooks.hookmain import begin  # noqa: E402
from sessionhooks.io import read_payload  # noqa: E402
from sessionhooks.store import head_commit  # noqa: E402
from sessionhooks.sweep import any_in_progress  # noqa: E402
from sessionhooks.vaultcli import VaultCLI  # noqa: E402

if TYPE_CHECKING:
    import logging

    from sessionhooks.config import SessionMemoryConfig
    from sessionhooks.store import Store

# Start sources that carry on a session already recorded rather than opening a new
# one. Re-recording the base commit for either would move it forward past work the
# session's log has already reported, cutting that log's span down to the tail.
CONTINUATION_SOURCES = frozenset({"resume", "compact"})


# The CLI's own guidance block names the commands a session needs. Naming the skill
# that carries the rest is this layer's job: a CLI installed without this plugin has
# no such skill, so a reference from there would point at nothing.
SKILL_POINTER = "The `cli` skill carries the full command surface for this vault."

LOCAL_LINKED_NOTE = (
    "This checkout's CLAUDE.local.md was linked from the vault during this session start, "
    "so Claude Code did not load it for this session. It loads on its own from the next "
    "session. Its content follows."
)

LOCAL_PENDING_NOTE = (
    "Local files in this checkout are waiting on a decision about the vault. Before other "
    "work, ask the person each question below, one at a time, in these words. Copy or "
    "overwrite nothing until they answer. A no to an adopt question leaves the file alone "
    "and is recorded so it is never asked again."
)

LOCAL_PENDING_COMMAND = (
    "Then, for each answer, run `{command} <the command shown> --cwd .`, naming every "
    "entry to adopt in one `local adopt` and every entry declined in one `local decline`. "
    "After the vault's settings copy is written into the checkout, tell the person to "
    "start a new session before approving anything, since Claude Code read its settings "
    "before this ran."
)

SETTINGS_PATH = ".claude/settings.local.json"

LOCAL_MANIFEST_NOTE = (
    "This project's local/entries.toml lists paths that cannot be linked, so they are "
    "missing from this checkout. Tell the person, and suggest `sessionmemory doctor`:"
)

LOCAL_DECLINED_NOTE = (
    "This project's local/declined.toml cannot be fully read, so entries the person "
    "declined can be asked about again, and `local decline` refuses until the file is "
    "fixed. Tell the person, and suggest `sessionmemory doctor`:"
)

# The order the log clause counts `local sync` statuses in, whatever order the CLI lists.
LOCAL_STATUSES = ("created", "present", "tracked", "removed", "conflict")


def _linked_entries_note(names: list[str]) -> str:
    """The line naming the skills and rules this start linked, and when they load."""
    note = (
        f"Linked from the vault during this session start: {', '.join(names)}. "
        "They load from the next session"
    )
    if any(name.startswith("rules/") for name in names):
        note += (
            "; a linked rule loads only after the one-time Claude Code approval "
            '"Allow external CLAUDE.md file imports?"'
        )
    return f"{note}."


# Every hook fails open, so a missing vault would otherwise mean session after session
# with no memory and no error. Both ways to name a vault are spelled out because the
# environment one silently fails to reach a session whose launching shell never sourced
# the export.
NO_VAULT_HINT = (
    "No vault is reachable, so this session has no memory and nothing it does will be "
    "recorded. Set SESSIONMEMORY_VAULT to the vault directory, or set root under [vault] "
    "in ~/.claude/sessionmemory.toml, then start a new session. A variable exported in a "
    "shell profile reaches only sessions launched from a shell that sourced it."
)


def _unregistered_hint(vault: VaultCLI) -> str:
    """The line a session in an unregistered project receives instead of memory."""
    return (
        "This project is not registered with the vault, so it has no memory yet. "
        f"Register it with: {vault.command} project register"
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


def _memory_block(
    cfg: SessionMemoryConfig, *, cwd: Path, notes: list[str] | None = None
) -> str | None:
    """The project's memory block, a hint about why there is none, or None when inject is off.

    `notes` collects one clause for the log naming what happened, when the caller wants it.
    """
    notes = notes if notes is not None else []
    if not cfg.inject_enabled:
        notes.append("inject off")
        return None
    vault = VaultCLI.discover(env=os.environ, configured=cfg.vault_root)
    if vault is None:
        notes.append("no memory: no vault")
        return NO_VAULT_HINT
    resolution = vault.resolve(cwd=cwd, env=os.environ)
    registered = resolution is not None and resolution.get("registered") is True
    slug: str | None = None
    if not registered:
        # A slug is permanent, so only a git working tree is registered unattended:
        # a session opened in a home or scratch directory must not file a project
        # named after it. Every other refusal belongs to the CLI.
        if resolution is None or not resolution.get("repo_root"):
            notes.append("unregistered")
            return _unregistered_hint(vault)
        slug = vault.register(cwd=cwd, env=os.environ)
        if slug is None:
            notes.append("registration refused")
            return _unregistered_hint(vault)
        notes.append(f"registered as {slug}")
    blocks: list[str] = []
    if slug is not None:
        blocks.append(_registered_note(slug))
    memory = vault.inject(cwd=cwd, env=os.environ)
    if memory:
        blocks.append(f"{memory}\n\n{SKILL_POINTER}")
        notes.append("memory injected")
    else:
        notes.append("nothing to inject")
    return "\n\n".join(blocks) or None


def _problems_block(payload: dict[str, Any] | None, *, key: str, note: str) -> list[str]:
    """The block naming the problems sync reported under `key`, or none."""
    problems = payload.get(key) if payload is not None else None
    if not isinstance(problems, list):
        return []
    lines = [f"- {line}" for line in problems if isinstance(line, str)]
    return ["\n".join([note, *lines])] if lines else []


def _local_block(
    vault: VaultCLI | None,
    cfg: SessionMemoryConfig,
    *,
    cwd: Path,
    clauses: list[str],
    log: logging.Logger,
) -> list[str]:
    """Link every local entry; the blocks this start owes the session for links it created.

    `CLAUDE.local.md`'s content is injected under `LOCAL_LINKED_NOTE`, and created skills
    and rules are named in one line. One clause counts each status for the log.
    """
    if vault is None or not cfg.local_link:
        return []
    payload = vault.sync_local(cwd=cwd, env=os.environ)
    if payload is not None and payload.get("status") == "refused":
        clauses.append("local refused")
        return []
    entries = payload.get("entries") if payload is not None else None
    if not isinstance(entries, list):
        clauses.append("local unavailable")
        return []
    results = [item for item in entries if isinstance(item, dict)]
    counts = {
        status: sum(1 for item in results if item.get("status") == status)
        for status in LOCAL_STATUSES
    }
    summary = ", ".join(f"{status} {n}" for status, n in counts.items() if n)
    pending = payload.get("pending") if payload is not None else None
    ask, waiting = _pending_block(
        pending if isinstance(pending, dict) else {}, command=vault.command
    )
    clause = f"local {summary or 'none'}"
    clauses.append(f"{clause}, pending {waiting}" if waiting else clause)
    for item in results:
        if item.get("status") == "conflict":
            log.info("local conflict: %s at %s", item.get("name"), item.get("link"))
    created = [item for item in results if item.get("status") == "created"]
    blocks: list[str] = []
    for item in created:
        if item.get("name") == shape.LOCAL_MD:
            blocks.extend(_local_md_block(item.get("target"), clauses=clauses, log=log))
    # Only skills and rules load from the next session; an adopted path is just a file.
    names = [
        name
        for item in created
        if item.get("kind") in {"skill", "rule"} and isinstance(name := item.get("name"), str)
    ]
    if names:
        blocks.append(_linked_entries_note(names))
    if ask:
        blocks.append(ask)
    blocks.extend(_problems_block(payload, key="manifest_problems", note=LOCAL_MANIFEST_NOTE))
    blocks.extend(_problems_block(payload, key="declined_problems", note=LOCAL_DECLINED_NOTE))
    return blocks


def _relpath(name: str) -> str:
    """The checkout path of an entry: skill and rule names are relative to `.claude/`."""
    return f".claude/{name}" if name.startswith(("skills/", "rules/")) else name


def _settings_lines(settings: dict[str, Any]) -> list[str]:
    """Phrase the one settings question that fits its state, with what each answer writes."""
    state = settings.get("state")
    mtimes: dict[str, object] = {}
    for side in ("checkout", "vault"):
        copy = settings.get(side)
        mtimes[side] = copy.get("mtime") if isinstance(copy, dict) else None
    if state == "checkout only":
        return [
            (
                f'- "Copy {SETTINGS_PATH} into the vault?" Yes copies it into the vault, and '
                "this checkout keeps its copy. (local sync --keep-settings checkout)"
            )
        ]
    if state == "vault only":
        return [
            (
                f'- "The vault has a {SETTINGS_PATH} for this project, but this checkout has '
                'none. Copy it into this checkout?" (local sync --keep-settings vault)'
            )
        ]
    lines = [
        (
            f'- "{SETTINGS_PATH} differs between this checkout and the vault. Which copy wins?" '
            f"Offer this checkout's (modified {mtimes['checkout']}), copied over the vault's "
            "(local sync --keep-settings checkout); the vault's "
            f"(modified {mtimes['vault']}), copied over this checkout's "
            "(local sync --keep-settings vault); or neither, leaving both alone."
        )
    ]
    summary = settings.get("summary")
    if isinstance(summary, list) and summary:
        lines.append("  - What each copy has that the other lacks:")
        lines.extend(f"    - {line}" for line in summary if isinstance(line, str))
    return lines


def _pending_block(pending: dict[str, Any], *, command: str) -> tuple[str | None, int]:
    """Build the block telling the agent to ask before adopting or overwriting anything.

    Returns:
        tuple[str | None, int]: The block, or None when nothing waits, and how many items
            wait: one per entry to adopt, plus one for settings.
    """
    adopt = [name for name in pending.get("adopt") or [] if isinstance(name, str)]
    settings = pending.get("settings") if isinstance(pending.get("settings"), dict) else None
    waiting = len(adopt) + (1 if settings else 0)
    if not waiting:
        return None, 0
    lines = [LOCAL_PENDING_NOTE, ""]
    lines.extend(
        f'- "Adopt {_relpath(name)} into the vault?" Yes moves it into the vault and links it '
        "back here; no keeps it here and stops asking. "
        f"(yes: local adopt {shlex.quote(name)}, no: local decline {shlex.quote(name)})"
        for name in adopt
    )
    if settings:
        lines.extend(_settings_lines(settings))
    lines.extend(["", LOCAL_PENDING_COMMAND.format(command=command)])
    return "\n".join(lines), waiting


def _local_md_block(target: object, *, clauses: list[str], log: logging.Logger) -> list[str]:
    """The block carrying a newly linked CLAUDE.local.md's content, or none when unreadable."""
    if not isinstance(target, str):
        return []
    try:
        content = Path(target).read_text(encoding="utf-8", errors="replace")
    except OSError:
        log.exception("local copy unreadable")
        clauses.append("local unreadable")
        return []
    return [f"{LOCAL_LINKED_NOTE}\n\n{content}"]


def _run(  # pragma: no cover - runs only as a subprocess, where coverage cannot see it
    payload: dict[str, Any],
    cfg: SessionMemoryConfig,
    *,
    cwd: Path,
    store: Store,
    log: logging.Logger,
) -> None:
    """Inject the handoff (if any) and memory for the current project."""
    clauses: list[str] = [f"source {payload.get('source') or 'unknown'}"]

    # Commit whatever the last session's sweep and any hand edits left behind, so
    # the vault's history is never more than one session behind its files, in
    # every case SessionEnd does. Skipped while any project's sweep runs: the
    # commit stages the whole vault, and the worker commits when it finishes.
    vault = VaultCLI.discover(env=os.environ, configured=cfg.vault_root)
    if vault is None:
        clauses.extend(["no vault", "commit skipped: no vault"])
    else:
        clauses.append("cli on PATH" if vault.on_path else "cli shim")
        if any_in_progress(os.environ):
            clauses.append("commit skipped: sweep in progress")
        else:
            clauses.append(vault.commit(env=os.environ).describe())

    # Consume the handoff on any start except `resume` (which may be the same session
    # that wrote it). A denylist, not an allowlist of known sources, keeps this working
    # if upstream adds a start source, and consumes rather than stranding the baton on
    # an unknown or missing source.
    source = payload.get("source")
    consume_handoff = source != "resume"
    if not (consume_handoff or cfg.inject_enabled or cfg.sweep_enabled or cfg.local_link):
        log.info("started (%s)", ", ".join([*clauses, "nothing to do"]))
        return  # resume with everything off: nothing left to do

    blocks: list[str] = []

    # Handoff first (freshest, most task-specific) and independent of inject config,
    # since the user explicitly created it. Read now, delete only after a clean emit.
    handoff_text = store.read_handoff() if consume_handoff else None
    if handoff_text:
        blocks.append(handoff_text)
        clauses.append("handoff consumed")

    # Tied to the sweep toggle, not the inject one: a user who turns injection off
    # still gets a sweep, and it is the sweep that reads this state.
    if cfg.sweep_enabled:
        _record_session_state(store, payload=payload, cwd=cwd)

    memory = _memory_block(cfg, cwd=cwd, notes=clauses)
    if memory:
        blocks.append(memory)

    # After the memory block so a project registered at this start can link.
    blocks.extend(_local_block(vault, cfg, cwd=cwd, clauses=clauses, log=log))

    log.info("started (%s)", ", ".join(clauses))

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
        log.warning("inject not emitted: stdout closed")
        return
    if handoff_text:
        store.delete_handoff()


def main() -> None:  # pragma: no cover - runs only as a subprocess, where coverage cannot see it
    """Configure the log, then run the hook body with its crash on record."""
    if is_headless():
        return
    payload = read_payload()
    # Bound ahead of the try: a crash before begin() configures the log still has
    # something to report to, the import-time NullHandler dropping it quietly.
    logger = log.logger()
    try:
        ctx = begin("sessionstart", payload=payload, env=os.environ)
        _run(ctx.payload, ctx.config, cwd=ctx.cwd, store=ctx.store, log=ctx.log)
    except Exception:
        logger.exception("hook failed")


if __name__ == "__main__":  # pragma: no cover - runs only as a subprocess
    with contextlib.suppress(Exception):  # fail-open: a hook never wedges the session
        main()
    sys.exit(0)
