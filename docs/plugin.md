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

The hooks do not control where other skills and plugins write their plans and specs. To
send those files to the vault, add the rules in
[Add rules to your CLAUDE.md](../README.md#add-rules-to-your-claudemd).

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

| Event          | What happens                                                                              |
| -------------- | ----------------------------------------------------------------------------------------- |
| `SessionStart` | Commits the vault, consumes any handoff, injects this project's memory, links local files |
| `SessionEnd`   | Triggers the sweep, then commits the vault                                                |
| `PreCompact`   | Triggers the sweep before compaction discards the context it reads                        |

### SessionStart

The hook commits whatever the last session and any hand edits left in the vault, so the
vault's history is never more than one session behind its files. Then it runs
`sessionmemory inject` for the current directory and returns the block as additional
context. It also records two things the sweep cannot recover later: the transcript path,
so the sweep finds it after a `/clear`, and the commit the repository was on, so the
session log reports the whole span.

When the directory is a git working tree that the vault has never seen, the hook
registers it through `sessionmemory project register` before it injects. The block
then opens with the slug the repository was filed under:

```
This repository was registered with the vault as project 'invoice-api'.
```

The block ends with a review nudge when the project holds at least 10 learnings and was
never reviewed or was last reviewed more than 30 days ago:

```
  Learnings last reviewed: never. At a natural stopping point, suggest /sessionmemory:review to the user.
```

`/sessionmemory:review` records the date as its last step with
`sessionmemory project mark-reviewed`, which stores `reviewed = "YYYY-MM-DD"` on the
project's entry in `_system/registry.toml`. The nudge stops until the date is more than
30 days old. The thresholds are fixed and are not settings.

A directory outside git is never registered by the hook. A slug is permanent once pages
carry it. A session opened in a home directory or a scratch folder must not leave a
project named after it in the vault. Such a directory, and a repository the CLI refuses
to register, receive the command instead:

```
This project is not registered with the vault, so it has no memory yet. Register it with: sessionmemory project register
```

The hint spells the CLI the way the session can run it: by name when the tool on `PATH`
passed the handshake, and by the cached copy's absolute path otherwise.

When the hook finds no vault at all, the session receives this instead of memory, since
the sweep at the end of the session will find none either:

```
No vault is reachable, so this session has no memory and nothing it does will be recorded. Set SESSIONMEMORY_VAULT to the vault directory, or set root under [vault] in ~/.claude/sessionmemory.toml, then start a new session. A variable exported in a shell profile reaches only sessions launched from a shell that sourced it.
```

The hook's timeout is 110 seconds, and it budgets its own worst case: resolving the
project root 5s, reading the head commit 5s, two version handshakes 10s, the vault commit
45s, one registration check 5s, one registration 5s, `sessionmemory local sync` 5s, and
`sessionmemory inject` 25s, which is 105 seconds in all. Injection reads no index and
loads no model, so a first session never waits on the embedding model download.

#### Syncing local files

After it registers the project and injects the memory block, the hook runs
`sessionmemory local sync --json` for the current directory. The command links each
`CLAUDE.local.md`, skill, rule, and adopted path that the vault holds for the project and
that the checkout does not have yet (see [Local files](concepts.md#local-files)). A new git
worktree, a fresh clone, and a second machine therefore receive them without any action
from you. The hook runs in any registered project, in a git work tree or not, whatever
starts the session, so a `/clear` in a worktree that you created during a session links
that worktree too.

If `local/entries.toml` or `local/declined.toml` holds a line that the command cannot
use, the hook adds a block that names the line and tells the agent to tell you.

The hook never replaces a file or folder that holds content, and it never copies
`settings.local.json`. A real `CLAUDE.local.md`, skill, or rule in the checkout stays where
it is until you choose to adopt it. See [`sessionmemory local`](cli.md#sessionmemory-local)
for the states and the commands.

When an entry waits to be adopted, or the two copies of `settings.local.json` differ or
exist on one side only, the hook adds a block that tells the agent to ask you before
other work. The block gives the agent one plain question per item, the same question
that `local sync` asks on a terminal, and the command that each answer maps to. For
differing settings, it also gives the modification time of each copy and what each copy
has that the other lacks:

```
Local files in this checkout are waiting on a decision about the vault. Before other work, ask the person each question below, one at a time, in these words. Copy or overwrite nothing until they answer. A no to an adopt question leaves the file alone and is recorded so it is never asked again.

- "Adopt .claude/rules/tickets.md into the vault?" Yes moves it into the vault and links it back here; no keeps it here and stops asking. (yes: local adopt rules/tickets.md, no: local decline rules/tickets.md)
- ".claude/settings.local.json differs between this checkout and the vault. Which copy wins?" Offer this checkout's (modified 2026-10-26T17:09:47Z), copied over the vault's (local sync --keep-settings checkout); the vault's (modified 2026-10-26T17:09:23Z), copied over this checkout's (local sync --keep-settings vault); or neither, leaving both alone.
  - What each copy has that the other lacks:
    - permissions.allow: 1 only in the checkout (Bash(just docs))
    - enabledPlugins: only in the checkout

Then, for each answer, run `sessionmemory <the command shown> --cwd .`, naming every entry to adopt in one `local adopt` and every entry declined in one `local decline`. After the vault's settings copy is written into the checkout, tell the person to start a new session before approving anything, since Claude Code read its settings before this ran.
```

The agent adopts and copies nothing until you answer. Every session start asks again
while the item waits. An entry that you decline is recorded with
[`local decline`](cli.md#sessionmemory-local-decline), and no session asks about it again.

Claude Code reads its memory files and skills before `SessionStart` runs. A link that the
hook creates therefore takes effect from the next session. To cover the session that made
the link, the hook adds the content of a newly linked `CLAUDE.local.md` once, in a block
after the memory block, under this line:

```
This checkout's CLAUDE.local.md was linked from the vault during this session start, so Claude Code did not load it for this session. It loads on its own from the next session. Its content follows.
```

This is the one place where the content of a file enters the block. The file is not a page
and not memory. It is the file that Claude Code would have loaded itself.

A newly linked skill or rule is named, not injected, since its body would cost context
that the session can leave unused:

```
Linked from the vault during this session start: skills/deploy-check, rules/style.md. They load from the next session; a linked rule loads only after the one-time Claude Code approval "Allow external CLAUDE.md file imports?".
```

Claude Code treats a linked rule as an external import. Until you approve that dialog once
in an interactive session in the checkout, the rule does not load, and a headless session
skips it. A linked skill needs no approval.

The hook writes one clause to the start's log line that counts each status, such as
`local created 2, present 1, conflict 1`, and logs each conflict on its own line with the
entry name and path. When anything waits on you, the clause ends with its count, such as
`local present 3, pending 2`. The other clauses are these:

| Clause              | Meaning                                                                                                                        |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `local none`        | The vault holds nothing for the project                                                                                        |
| `local refused`     | The command exited 1 and printed nothing, for example because the directory is not a registered project                        |
| `local unavailable` | The command could not start, ran out of time, exited with a code other than 0 or 1, or printed no `entries` list               |
| `local unreadable`  | The `CLAUDE.local.md` link was made, but the hook could not read the vault copy                                                |

The hook links even when `inject.enabled` is false. Set `link = false` under `[local]` to
stop it. That also stops the block that asks about waiting entries and settings. The
setting does not affect the `sessionmemory local` commands. Update the plugin and the CLI
together: the hook calls `local sync`, which an older CLI does not have.

### SessionEnd

The hook triggers the sweep when the sweep is enabled, then commits the vault whether or
not the sweep ran. A session that recorded nothing still lands any outstanding change.

Claude Code gives all SessionEnd hooks together 1.5 seconds by default, and a plugin
cannot raise that limit. The hook runs in about a third of a second, but a slow
first start can go over the limit. Claude Code then stops the hook and prints
`SessionEnd hook [...] failed: Hook cancelled`. Nothing is lost: the next session's
start commits what the hook left behind.

To give SessionEnd hooks more time, set `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` in the
environment Claude Code starts in. A value of `5000` is enough:

```bash
export CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS=5000
```

### PreCompact

The hook triggers the same sweep before compaction discards the transcript it reads.
Nothing is committed here, and the timeout is 10 seconds.

### Committing the vault

A commit stages everything under the vault except the derived index files and each
project's `plans/` folder, and writes one message that says what changed.

A global gitignore applies inside the vault repository too, and it can name a project's
`local/` folder or a file in it: `local/`, `CLAUDE.local.md`, or a skill's `env/` or
`*.log`. The vault's own `.gitignore` takes precedence. It re-includes the
`projects/*/local/` folder itself, because git cannot re-include a file under an ignored
folder, and then every file under it. Then it drops `.DS_Store`, `__pycache__/`,
`node_modules/`, `.venv/`, and `venv/`. `sessionmemory init` writes these lines, and the
`local` commands add any that an older vault lacks. The commit also force-stages each
project's `CLAUDE.local.md` as a backstop. Git hides an ignored file, or the ignored
folder that holds it, from the check that decides whether anything changed. The hook
looks for that file itself, so it does not skip the commit as clean.

Plans are scratch for the session that writes them, so they stay out of history even when the
vault's `.gitignore` predates that rule. A plan already tracked stays tracked, but the
hooks never commit an edit to it. Commit the plan by hand to keep an edit in history.

The subject names the project and counts the changes by kind. The body lists every path,
and opens with the summary of the session's log when a sweep wrote the commit:

```text
invoice-api: add 1 log, 2 learnings; edit backlog, 1 spec

Made the charge handler idempotent so a retried webhook never double-bills a customer

edit: projects/invoice-api/backlog.md
add: projects/invoice-api/learnings/retries-need-idempotency-keys.md
add: projects/invoice-api/learnings/webhooks-arrive-out-of-order.md
add: projects/invoice-api/logs/2026-10-13-09-41.md
edit: projects/invoice-api/specs/2026-10-13-retries.md
```

A commit that touches several projects names them and counts every changed file, not
the kind of change, the vault's own files included:

```text
billing, invoice-api: update 4 files

edit: _system/registry.toml
add: projects/billing/learnings/invoices-round-half-up.md
add: projects/invoice-api/learnings/idempotency-keys-expire-after-a-day.md
add: projects/invoice-api/logs/2026-10-13-14-05.md
```

A change to the vault's own files alone reads `vault: edit 1 file`. A subject over 70
characters is cut at a separator and ends with `...`.

Four situations are skipped rather than committed. A merge, rebase, cherry-pick, or
revert in progress is skipped, because a commit landing in the middle of one bakes
half-resolved state into history. A detached `HEAD` is skipped, because such a commit is
reachable from nothing once a branch is checked out again. A commit is skipped while any
project's sweep is running, because a commit stages the whole vault and would record that
sweep's writes before the sweep checks them. A worker that finishes while another
project's sweep still runs logs `commit skipped: another sweep in progress`, so when two
sweeps finish at the same moment both may skip, and the next hook commits what they left.
Two hooks reaching git at the same moment race on git's own `index.lock`, and the loser
skips. In every case the next hook commits what was left behind.

## The sweep's four jobs

`SessionEnd` and `PreCompact` start the same pass. The gating runs inline, and the heavy
part runs in a detached worker that outlives session teardown, so nothing waits on it.
The worker hands the transcript to a headless `claude -p` run whose working directory is
this project's vault folder.

The pass first decides whether the session is worth recording at all. A session that was
routine maintenance, or a couple of commands, or abandoned before anything was decided,
is recorded as nothing at all. Then it does four jobs, in order:

1. **Learnings.** One page per durable fact that a future session in this repository
   would otherwise get wrong. Before writing one, the pass reads the repository and the
   session's own commits, and searches the repository's `CLAUDE.md`, `AGENTS.md`,
   `README.md`, and `docs/` for the fact. It drops any fact the code, a comment, a config
   value, a test, or one of those documents already states. A fact found only in a
   commit message still counts as unrecorded, since sessions rarely read old commit
   messages. A decision the session made, and the
   alternatives it rejected, stay in the commit that made them. A fact every session
   needs belongs in the project's `CLAUDE.md`, and the pass records it as a `docs`
   backlog line rather than a page. A fact already recorded is refined in place, never
   written a second time.
2. **Deferred work.** Concrete work that was decided and not done becomes one line in
   `backlog.md`, written through `sessionmemory new backlog`. Work the transcript shows
   was finished has its line deleted.
3. **Specs.** For a spec in `specs/` that the session worked on, the pass keeps three
   frontmatter keys current: `status: shipped` when the work merged, `status: superseded`
   with `superseded_by` when a newer spec replaced it, and a one-sentence `summary` for a
   spec that has neither a `summary` nor a paragraph under its title. A spec with no
   frontmatter block gets one holding only the keys the pass sets. It never changes a
   spec's body.
4. **The session log.** One page per session, written through `sessionmemory log`, which
   replaces the page's whole body on every call. The page is titled with the date and
   clock time the session began, in local time, so two sessions on one day are told
   apart by when each started.

Most sessions produce nothing for jobs 1 to 3, and that is the correct outcome. Three
floors keep trivial sessions out of the vault, and below any one of them the whole pass
is skipped. Two of the three are measured on your messages alone, so a long agent reply
to one short instruction never clears them. A slash command that sends the agent a
prompt, such as a skill or `/security-review`, counts as a message from you, measured by
the line you typed, `/refactor site/*` for example. A command that manages Claude Code
itself, such as `/plugin`, `/model`, `/context`, or `/clear`, runs without prompting the
agent and does not count.

Only one sweep runs at a time for each project. When a second session in the same project
ends while a sweep is still running, its worker waits for the first to finish and then
runs, rather than being dropped. It checks every 5 seconds and waits for four times
`sweep.timeout` plus 120 seconds, or one hour, whichever is longer: one hour at the
default timeout of 600 seconds, enough for several queued sweeps at any setting. The
hooks log records each step: `sweep queued: another sweep holds the lock` when the worker
starts to wait, `sweep dequeued after about Ns` when it gets its turn, and the warning
`sweep skipped: lock still held after Ns` when it gives up. The lock is released when the
worker exits for any reason, so a crashed sweep never blocks the next one. A lock file
that cannot be opened at all ends the wait at once, with the warning
`sweep skipped: lock unavailable: <error>`.

### What the sweep may write

The pass may create and edit any file inside its own project's folder, which is what a
later pass strengthening a memory means. It is told never to delete a page, because
retiring one is a decision a person makes in review.

Two backstops run after the model finishes, or after its timeout stops it. A write the
model reported outside the project folder, including one reported before a timeout, is
restored from the vault's git history, and a file git cannot restore is moved into a
quarantine directory rather than deleted. Every file under the project folder whose
content changed during the run is then scanned for secret-shaped content and redacted in
place, which catches a write made through Bash that no tool call reported.

A third check enforces the shape of what the pass wrote. A new file in `learnings/` must
have a conformant name and frontmatter holding only `title`, `uuid`, `summary`,
`created`, and `updated`, with a non-blank `title` and `summary`. A new file in `logs/`
must have a conformant name. A new file in a sub-directory of either folder is nested,
since nothing reads a page there. A new file directly in the project folder, other than
`backlog.md`, is stray. Any of these is moved into the quarantine directory. An existing
learning the pass rewrote into another shape, such as another tool's frontmatter, is
restored from the vault's git history instead, or quarantined when git has no copy of it.
A learning the pass created or changed that cannot be read is quarantined whether it is
new or not, since its content is unknown and a file git cannot read would stop the next
commit. Files a sync tool, an editor, or the operating system leaves behind, such as
`.DS_Store`, a Syncthing conflict copy, or a name ending in `~`, are left alone.

Every file under the project's `local/` folder belongs to you, and the sweep prompt tells
the pass never to write, move, or delete anything there. A file that the pass modified is
put back to the content it had when the pass started, so edits you had not committed yet
survive. The content the pass displaces is first copied into the quarantine directory,
and the log note names the copy. A deleted file is restored the same way. A file that
appeared during the pass and is not in the vault's git history stays in place, and the
log note names it. The pass cannot be told apart from a `local adopt` in another session,
and removing the file can delete the only copy of an entry. The secret scrub skips
`local/`, so the restored bytes are exactly yours.

A Bash write elsewhere in the vault, a sibling project's folder foremost, is caught by
neither backstop, and the next commit records it. This is a real control against a
confused agent and a partial one against a deliberately steered one. Git history is what
recovers the file.

### Which credentials the sweep uses

The pass runs `claude -p` with the environment of the session that ended, so by default
it authenticates the same way that session did: an exported `ANTHROPIC_API_KEY`, then an
`apiKeyHelper` in your Claude Code settings, then your `claude` login.

To bill the pass to its own key, export `SESSIONMEMORY_ANTHROPIC_API_KEY` in the
environment Claude Code starts in. When it is set and not blank, the pass runs with that
value as its `ANTHROPIC_API_KEY`, ahead of all three. The pass also drops
`ANTHROPIC_AUTH_TOKEN` and the `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, and
`CLAUDE_CODE_USE_FOUNDRY` switches, because `claude` ranks each of them above an API key.
A signed-in Claude apps gateway outranks every credential and still wins. Your session
itself never reads the variable.

### Sweeping by hand

Run `/sessionmemory:sweep` to do the sweep in your own session instead of in a separate
`claude -p` run. The agent that did the work records it, with the same instructions, the
same four jobs, and the same backstops. The pass uses the credentials and the model of
your session.

The skill runs in three parts:

1. `hooks/sweep-prompt.py` prints the sweep instructions for this project and this
   session, and records the project folder before any write.
2. The agent follows the instructions.
3. `hooks/sweep-check.py` runs the backstops from
   [What the sweep may write](#what-the-sweep-may-write) on every file that changed in
   the project folder after part 1.

When the check script exits 0, the transcript records a finished manual sweep. The
automatic sweep at session end or before a compaction then judges only the messages
after it, against the same three floors. A session that ends right after a manual sweep
is below the floors, so the automatic sweep skips it and the hooks log shows
`sweep skipped: below threshold (...)`. If you continue to work after a manual sweep, the
automatic sweep records only that later work.

If the manual sweep is interrupted, or the check script fails, the transcript records no
finished sweep. The automatic sweep then judges the whole session as usual.

A manual sweep and a later automatic sweep of the same session write the same log page.
The automatic sweep receives the page that the manual sweep wrote and carries its dated
blocks forward.

The manual sweep reads the project's learnings and backlog into your session, up to
50,000 characters. If your session is close to its context limit, run the sweep before
you run `/compact`.

## Settings

Settings live in `sessionmemory.toml`. Two files are read, and the later one wins per
key: `~/.claude/sessionmemory.toml`, then `<project>/.claude/sessionmemory.toml`.
Every key is optional, and omitting the file uses the defaults below. A file that cannot
be read or parsed is ignored with a warning, so a broken configuration never wedges a
session.

| Key                       | Default                                  | What it does                                                                              |
| ------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------------- |
| `vault.root`              | unset                                    | Where the vault is, for the hooks and the CLI, when `SESSIONMEMORY_VAULT` is not exported |
| `inject.enabled`          | `true`                                   | Set false to stop memory injection at session start                                       |
| `sweep.enabled`           | `true`                                   | Set false to stop the end-of-session pass                                                 |
| `sweep.model`             | `claude-sonnet-4-6`                      | The model the pass runs on                                                                |
| `sweep.min_exchanges`     | `10`                                     | Skip the pass below this many real messages, yours and the agent's                        |
| `sweep.min_user_messages` | `3`                                      | Skip the pass below this many messages from you                                           |
| `sweep.min_user_chars`    | `200`                                    | Skip the pass below this many characters from you, in total                               |
| `sweep.timeout`           | `600`                                    | Seconds the pass may run before it is killed                                              |
| `sweep.save_transcript`   | `true`                                   | Save the pass's own session, so its API usage is auditable                                |
| `local.link`              | `true`                                   | Set false to stop the session start from linking local files                              |
| `log.path`                | `~/.local/state/sessionmemory/hooks.log` | Where every hook on this machine logs; `hooks/vault-path.py --log` prints it              |
| `log.level`               | `info`                                   | `debug`, `info`, `warning`, `error`, or `off`                                             |

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

| Command                  | What it does                                                                  |
| ------------------------ | ----------------------------------------------------------------------------- |
| `/sessionmemory:review`  | Curates the learnings and reconciles log commits with the trunk               |
| `/sessionmemory:backlog` | Triages `backlog.md`, then ranks what is left by impact and effort            |
| `/sessionmemory:handoff` | Writes a consume-once handoff of the current task, for the next session       |
| `/sessionmemory:sweep`   | Records this session into the vault now, in this session                      |
| `/sessionmemory:cli`     | The command surface, loaded when a session needs to read or write the vault   |

`cli` is the one skill a session can reach on its own. The other four run only
when you ask for them. [Sweeping by hand](#sweeping-by-hand) describes `sweep`.

`review` handles the learnings field and the commit lists in the logs. `backlog`
handles `backlog.md`. Neither touches the other's files, except that `review` adds a
backlog line for a page that turns out to be deferred work. For the logs, `review` runs
[`sessionmemory reconcile`](cli.md#sessionmemory-reconcile), which rewrites only the
`## Changes` section of a log. `backlog` stops to ask before closing or removing an
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

| Agent                          | What it judges                                                      |
| ------------------------------ | ------------------------------------------------------------------- |
| `memory-entry-reviewer`        | Whether a page is correct, at the right altitude, and worth keeping |
| `redundancy-reviewer`          | Which pages in one field overlap enough to merge                    |
| `backlog-validity-reviewer`    | Whether a backlog item is done, obsolete, drifted, or still valid   |
| `backlog-opportunity-reviewer` | How much a valid backlog item is worth, by impact against effort    |

`redundancy-reviewer` is the only one with a whole-field view. A flat field accumulates
near-duplicates, and nothing else in the system consolidates them.

## Troubleshooting

**A sweep recorded nothing.** Read the hooks log, which `hooks/vault-path.py --log`
prints and which defaults to `~/.local/state/sessionmemory/hooks.log`. Every hook writes
one line per decision, so a short session shows as `sweep skipped: below threshold` with
each measure against its floor, and a session the worker swept shows as `sweep finished`
with the files it changed and the commit it made. A worker that cannot start the pass
logs one of these reasons, exactly as the log prints it:

- `sweep skipped: no CLI: neither PATH nor the plugin shim`
- `sweep skipped: project timed out after <n>s`, where the project lookup has 120 seconds
- `sweep skipped: project could not start: <error>`
- `sweep skipped: project exited <code>: <stderr>`
- `sweep skipped: project not registered`
- `sweep skipped: project printed no JSON`
- `sweep skipped: project payload lacked <key>`
- `sweep skipped: lock unavailable: <error>`
- `sweep skipped: lock still held after <n>s`

A `sweep failed` line carries the exit code, the start of the result text, and the tail
of stderr, which is what tells a missing `claude` binary from a timeout or a rejected key.
It then names the files the run changed and the commit, as a `sweep finished` line does.
The log rotates at 1 MB with two backups kept, so it holds weeks of ordinary use. Set
`log.level` to `debug` to see the version handshake and each git call.

```text
2026-09-07 12:58:27 INFO  [sessionend  ] invoice-api: sweep skipped: below threshold (exchanges 4/10, user messages 2/3, user chars 59/400) (session=05ecdee7)
2026-09-07 12:58:27 INFO  [sessionend  ] invoice-api: commit skipped: clean (session=05ecdee7)
```

**A session starts with no memory.** If it started with the "No vault is reachable" hint,
the hook is not finding the vault root. A `SESSIONMEMORY_VAULT` exported from a shell
profile reaches only sessions launched from a shell that sourced it, so a terminal opened
before the export was added, and any GUI launcher, starts sessions without it. Set
`vault.root` in `~/.claude/sessionmemory.toml`, which every session reads. Without the
hint, run `sessionmemory project` in that repository. An unregistered directory exits 1
and names the command that registers it. The hook registers a git repository on its own,
so a repository that stays unregistered is one the CLI refused. Run
`sessionmemory project register --cwd .` by hand to see the reason, which is usually a
slug another project holds.

**Search misses a page you can see.** The index is behind its pages. Run
`sessionmemory reindex --cwd <your repo>`, or delete the `*.sqlite3` file in the field and
run it again. `sessionmemory doctor` reports an index that is behind or unreadable, and
`sessionmemory reindex --all` clears every such finding in one run.

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
