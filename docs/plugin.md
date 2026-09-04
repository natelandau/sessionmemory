# The Claude Code plugin

`sessionmemory` is the half of this repository that makes the vault automatic. It
gives a session the project's memory when the session starts, and records what the
session was worth keeping when it ends.

The CLI and the plugin ship from one repository and install as one unit, so neither half
can drift from the other.

## Install

Install the CLI and create a vault first, as [the README](../README.md) describes. Then,
in Claude Code, add this repository as a marketplace and install the plugin from it:

```
/plugin marketplace add natelandau/sessionmemory
/plugin install sessionmemory@sessionmemory
```

Claude Code copies the plugin into
`~/.claude/plugins/cache/sessionmemory/sessionmemory/<version>/`. A release reaches the
hooks through `/plugin update sessionmemory@sessionmemory`, or a reinstall.

### Which CLI the hooks run

Every hook, skill, and agent runs the `sessionmemory` on your `PATH` when it passes a
version handshake, and otherwise the copy of the CLI inside the plugin cache. The
handshake is one call to `sessionmemory --version` at the start of each hook. It passes
when the tool's version is at or past the plugin's own, which is the version in the
`pyproject.toml` beside the cached copy. The hooks hard-code the flags and payloads of the
CLI they shipped with, and every hook fails open, so an older tool would mean a session
with no memory and no error. The fallback is that older case: the plugin runs its own
copy, through `bin/sessionmemory` in the cache, in a Python environment that the first
such session builds.

The practical rule is to keep the two in step. After `/plugin update`, run
`uv tool upgrade sessionmemory`. Nothing breaks in between, since the fallback covers the
gap, but every session in the gap pays for the second environment.

### Point the hooks at your vault

A hook does not start from an interactive shell. Claude Code launches a session from a
GUI, an IDE extension, or a scheduled task without reading `.zshrc` or `.bashrc`, so a
root exported there is invisible to it. Record the root in a configuration file as well:

```toml
# ~/.claude/sessionmemory.toml
[vault]
root = "~/repos/my-vault"
```

`SESSIONMEMORY_VAULT` still wins when both are set. The CLI reads the same key with the
same precedence, so one line in this file configures both halves. This is the one setting
a plugin install most needs. A session that finds neither one runs with no memory, and
says nothing about it.

## What runs, and when

Three hooks, and every one of them fails open. An error exits 0 rather than wedging your
session.

| Event          | What happens                                                            |
| -------------- | ----------------------------------------------------------------------- |
| `SessionStart` | Commits the vault, injects this project's memory, consumes any handoff  |
| `SessionEnd`   | Triggers the sweep, then commits the vault                              |
| `PreCompact`   | Triggers the sweep before compaction discards the context it reads      |

### SessionStart

The hook commits whatever the last session and any hand edits left in the vault, so the
vault's history is never more than one session behind its files. Then it runs
`sessionmemory inject` for the current directory and returns the block as additional
context. It also records two things the sweep cannot recover later: the transcript path,
so the sweep finds it after a `/clear`, and the commit the repository was on, so the
session log reports the whole span.

When the directory is a git working tree that the vault has never seen, the hook
registers it through `sessionmemory project --register` before it injects. The block
then opens with the slug the repository was filed under:

```
This repository was registered with the vault as project 'invoice-api'.
```

A directory outside git is never registered by the hook. A slug is permanent once pages
carry it. A session opened in a home directory or a scratch folder must not leave a
project named after it in the vault. Such a directory, and a repository the CLI refuses
to register, receive the command instead:

```
This project is not registered with the vault, so it has no memory yet. Register it with: sessionmemory project --register
```

The hint spells the CLI the way the session can run it: by name when the tool on `PATH`
passed the handshake, and by the cached copy's absolute path otherwise.

The hook's timeout is 100 seconds, and it budgets its own worst case: resolving the
project root 5s, reading the head commit 5s, two version handshakes 10s, the vault commit
35s, one registration check 5s, one registration 5s, and `sessionmemory inject` 25s,
which is 90 seconds in all. Injection reads no index and loads no model, so a first
session never waits on the embedding model download.

### SessionEnd

The hook triggers the sweep when the sweep is enabled, then commits the vault whether or
not the sweep ran. A session that recorded nothing still lands any outstanding change.
The timeout is 60 seconds, which covers the commit's 35 second worst case with headroom.

### PreCompact

The hook triggers the same sweep before compaction discards the transcript it reads.
Nothing is committed here, and the timeout is 10 seconds.

### Committing the vault

A commit stages everything under the vault except the derived index files, and writes one
message of the form `chore(vault): checkpoint 2026-09-02 09:44`.

Four situations are skipped rather than committed. A merge, rebase, cherry-pick, or
revert in progress is skipped, because a commit landing in the middle of one bakes
half-resolved state into history. A detached `HEAD` is skipped, because such a commit is
reachable from nothing once a branch is checked out again. A sweep worker that still
holds a fresh lock is skipped, because the worker commits its own writes when it
finishes. Two hooks reaching git at the same moment race on git's own `index.lock`, and
the loser skips. In every case the next hook commits what was left behind.

## The sweep's three jobs

`SessionEnd` and `PreCompact` start the same pass. The gating runs inline, and the heavy
part runs in a detached worker that outlives session teardown, so nothing waits on it.
The worker hands the transcript to a headless `claude -p` run whose working directory is
this project's vault folder.

The pass first decides whether the session is worth recording at all. A session that was
routine maintenance, or a couple of commands, or abandoned before anything was decided,
is recorded as nothing at all. Then it does three jobs, in order:

1. **Learnings.** One page per durable fact that a future session in this repository
   would otherwise get wrong. A fact already recorded is refined in place, never written
   a second time.
2. **Deferred work.** Concrete work that was decided and not done becomes one line in
   `backlog.md`, written through `sessionmemory new backlog`. Work the transcript shows
   was finished has its line deleted.
3. **The session log.** One page per session, written through `sessionmemory log`, which
   replaces the page's whole body on every call. The page is titled with the date and
   clock time the session began, in local time, so two sessions on one day are told
   apart by when each started.

Most sessions produce nothing for jobs 1 and 2, and that is the correct outcome. Three
floors keep trivial sessions out of the vault, and below any one of them the whole pass
is skipped. Two of the three are measured on your messages alone, so a long agent reply
to one short instruction never clears them.

### What the sweep may write

The pass may create and edit any file inside its own project's folder, which is what a
later pass strengthening a memory means. It is told never to delete a page, because
retiring one is a decision a person makes in review.

Two backstops run after the model finishes. A write the model reported outside the
project folder is restored from the vault's git history, and a file git cannot restore is
moved into a quarantine directory rather than deleted. Every file under the project
folder modified since the run started is then scanned for secret-shaped content and
redacted in place, which catches a write made through Bash that no tool call reported.

A Bash write elsewhere in the vault, a sibling project's folder foremost, is caught by
neither backstop, and the next commit records it. This is a real control against a
confused agent and a partial one against a deliberately steered one. Git history is what
recovers the file.

## Settings

Settings live in `sessionmemory.toml`. Two files are read, and the later one wins per
key: `~/.claude/sessionmemory.toml`, then `<project>/.claude/sessionmemory.toml`.
Every key is optional, and omitting the file uses the defaults below. A file that cannot
be read or parsed is ignored with a warning, so a broken configuration never wedges a
session.

| Key                       | Default             | What it does                                                       |
| ------------------------- | ------------------- | ------------------------------------------------------------------ |
| `vault.root`              | unset               | Where the vault is, for the hooks and the CLI, when `SESSIONMEMORY_VAULT` is not exported |
| `inject.enabled`          | `true`              | Set false to stop memory injection at session start                 |
| `sweep.enabled`           | `true`              | Set false to stop the end-of-session pass                           |
| `sweep.model`             | `claude-sonnet-4-6` | The model the pass runs on                                          |
| `sweep.min_exchanges`     | `10`                | Skip the pass below this many real messages, yours and the agent's  |
| `sweep.min_user_messages` | `3`                 | Skip the pass below this many messages from you                     |
| `sweep.min_user_chars`    | `400`               | Skip the pass below this many characters from you, in total         |
| `sweep.save_transcript`   | `true`              | Save the pass's own session, so its API usage is auditable          |

`hooks/sessionmemory.toml.example` in this repository holds the same table as a file
you can copy.

To turn everything off for one repository, put this in
`<project>/.claude/sessionmemory.toml`:

```toml
[inject]
enabled = false

[sweep]
enabled = false
```

Both hooks still commit the vault with everything turned off, so a page you write by hand
still reaches git.

## Slash commands

The automatic pass adds and refines. Deciding that a page is wrong, finished, or
redundant is a judgment, so curation is yours to start.

| Command                           | What it does                                                                |
| --------------------------------- | --------------------------------------------------------------------------- |
| `/sessionmemory:review`  | Curates the learnings: sharpens weak titles, retires stale or redundant pages |
| `/sessionmemory:backlog` | Triages `backlog.md`, then ranks what is left by impact and effort           |
| `/sessionmemory:handoff` | Writes a consume-once handoff of the current task, for the next session      |
| `/sessionmemory:cli`    | The command surface, loaded when a session needs to read or write the vault  |

`cli` is the one skill a session can reach on its own. The other three run only
when you ask for them.

`review` handles the learnings field and `backlog` handles `backlog.md`; neither
touches the other's files, except that `review` adds a backlog line for a page that
turns out to be deferred work. `backlog` stops to ask before closing or removing an
item a reviewer judged with low confidence. Pass `--yes`, as in
`/sessionmemory:backlog --yes`, to apply those too and read them in the report
instead. Nothing else waits on you: every page edit and delete is applied outright,
since the vault is a git repository and a wrong one is reverted from its history.

`review` matters more than it looks. A page's title is what every session start
shows and its summary is what a search returns, so a weak title or a thin summary is a
memory that effectively does not exist.

`handoff` is a baton and not durable memory. It writes the state of the task you are
in the middle of to machine-local state, never to the vault. The next fresh session
receives it and deletes it. Run it before a `/compact` or a `/clear`.

## Review subagents

`review` and `backlog` dispatch read-only reviewer subagents, which judge and
never write. The skill collects their verdicts and applies the changes itself.

| Agent                          | What it judges                                                     |
| ------------------------------ | ------------------------------------------------------------------ |
| `memory-entry-reviewer`        | Whether a page is correct, at the right altitude, and worth keeping |
| `redundancy-reviewer`          | Which pages in one field overlap enough to merge                    |
| `backlog-validity-reviewer`    | Whether a backlog item is done, obsolete, drifted, or still valid   |
| `backlog-opportunity-reviewer` | How much a valid backlog item is worth, by impact against effort    |

`redundancy-reviewer` is the only one with a whole-field view. A flat field accumulates
near-duplicates, and nothing else in the system consolidates them.

## Troubleshooting

**A sweep recorded nothing.** Short sessions are skipped on purpose, so compare the
session against the three floors in the settings table. Every run also appends one line
to `sweep.log` in this project's machine-local state directory, which
`hooks/vault-path.py --state-dir` prints. The line carries the outcome, the files
written, and the tail of stderr when the run failed.

**A session starts with no memory.** Run `sessionmemory project` in that repository. An
unregistered directory exits 1 and names the command that registers it. The hook
registers a git repository on its own, so a repository that stays unregistered is one the
CLI refused. Run `sessionmemory project --register --cwd .` by hand to see the reason,
which is usually a slug another project holds. If the project is registered, run
`sessionmemory inject --cwd <your repo>` by hand from a shell. If that prints the block,
the hook is not finding the vault root, so set `vault.root` in
`~/.claude/sessionmemory.toml`.

**Search misses a page you can see.** The index is behind its pages. Run
`sessionmemory reindex --cwd <your repo>`, or delete the `*.sqlite3` file in the field and
run it again. `sessionmemory doctor` reports an index that is behind or unreadable.

## How the pieces fit

`bin/sessionmemory` is the only file in this repository that knows the CLI runs through
`uv`. It resolves its own location through any symlink chain, then runs the CLI from the
project it belongs to. Every hook, skill, and agent reaches the CLI through one
resolver, which prefers the tool on `PATH` and falls back to that path, so the invocation
strategy stays changeable in one place.

The hooks import nothing from `src/`. They are standalone `uv run --script` programs with
no dependencies of their own, and they reach the CLI the same way you would from a shell.
The CLI owns everything that knows what a page is: paths, filenames, frontmatter, and the
rendering of the session-start block. The plugin owns what a session is.
