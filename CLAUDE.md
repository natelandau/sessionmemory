# CLAUDE.md

## Overview

This project keeps a coding agent's durable memory as markdown. Each registered project
owns one folder in a vault, and inside it `learnings/` and `logs/` are fields: flat
directories of pages, each with its own deletable vector index. The CLI creates pages and
searches them by meaning, and does nothing an agent already does well with Read, Grep,
and Write.

## Running

Every command reads `SESSIONMEMORY_VAULT` to locate the vault, falls back to
`vault.root` in `~/.claude/sessionmemory.toml` when the variable is unset, and exits
non-zero when neither names a vault or the named directory is missing or uninitialized.
The fallback lives in `lib/config.vault_root` and reads that one key and nothing else;
every other setting in the file belongs to the hooks. `init` reads the root directly
rather than through the check every other command goes through, since `init` is what
makes that check pass. Point the variable at a throwaway directory when running by hand,
since it wins over the file.

```bash
export SESSIONMEMORY_VAULT=/tmp/scratch-vault && mkdir -p "$SESSIONMEMORY_VAULT"
uv run sessionmemory init
uv run sessionmemory [COMMAND]
uv run sessionmemory --help
```

`sessionmemory init` is a human bootstrap command, not something an agent runs: it takes a
directory, runs once per vault, and happens before `SESSIONMEMORY_VAULT` points anywhere
useful. A directory a human has been dropping notes into by hand holds files but no
`_system/vault.toml` marker; every other command refuses it rather than naming `--force`
as a next action, since scaffolding a vault into whatever directory an agent happens to be
pointed at is not something an error message should invite. Bringing such a directory
under `sessionmemory init` is a one-time step a human runs by hand:
`sessionmemory init --force <path>`. Nothing existing is overwritten.

## Development

```bash
uv sync                  # Install dependencies
uv run duty lint         # Run all linters (ruff, ty, typos, yamllint, shellcheck, prek)
uv run duty test         # Run tests with coverage
uv run ruff check src/   # Check code quality
uv run ruff format src/  # Format code
```

### CLI Wiring

The root `typer.Typer()` lives in `cli.py`, and its `@app.callback()` holds the global
`-v` flags. Entry point: `sessionmemory.cli:main` (configured in `pyproject.toml`).
Command modules live in `src/sessionmemory/commands/` and know nothing about file
formats; library modules in `src/sessionmemory/lib/` hold the logic and know nothing
about Typer.

Two shapes are in use, and which one a command takes depends on whether it has
sub-commands of its own:

- **A flat command is a plain function.** `project.py` exports `project_command` as an
  ordinary function, and `cli.py` registers it:

    ```python
    app.command("project", help="...")(project.project_command)
    ```

    Use this shape by default. A module may export several such functions.

- **A command group exports its own `typer.Typer()`.** `new.py` creates
  `app = typer.Typer(no_args_is_help=True, help="Create a learning, spec, or plan.")`,
  decorates each sub-command with `@app.command("learning")`, and `cli.py` mounts the
  group:

    ```python
    app.add_typer(new_commands.app, name="new")
    ```

    Use this shape only when the command genuinely takes a sub-command, as
    `sessionmemory new learning` does. The group carries `help=` on the `Typer()` call rather
    than a `@app.callback()`, since it has no options or behavior of its own.

### Command Docstrings Render as `--help`

Typer uses a command function's docstring as its `--help` text, and a `@app.callback()`
docstring as the app's own help. A Google-style `Args:`, `Returns:`, or `Raises:` section
in either one is printed to the user verbatim.

Ruff requires those sections wherever the signature calls for them, so the two rules
collide on any command that raises. Resolve it by registering the command with an
explicit `help=` string, which overrides the docstring for help output and leaves the
docstring free to document `Raises:` for a reader of the code. A command whose docstring
is a single line, such as `new_learning`, needs no `help=`.

### Console Output

- Output is handled by the external `nclutils` package via its `pp` printer. Never call
  `print()` or instantiate `rich.Console()` directly
- Import the printer: `from nclutils import pp`
    - `pp.step("message")`: context manager, spinner while running, `✓`/`✗` on completion
        - Use `s.sub("text")` inside `with pp.step(...) as s:` to queue sub-items
    - `pp.success(msg, details=[...])`: terminal success line with optional sub-items
    - `pp.info(msg)`: informational line
    - `pp.debug(msg)`: shown with `-v`
    - `pp.trace(msg)`: shown with `-vv`
    - `pp.dryrun(msg)`: always shown, prefixed `[dry-run]`
    - `pp.warning(msg, details=[...])` / `pp.error(msg, details=[...])`: stderr, pass a
      list for follow-up lines
- For tables/panels/direct Rich usage: `pp.console().print(...)`
- Verbosity set via `-v`/`-vv` flags on root command, wired through
  `pp.configure(verbosity=...)`

#### Machine-readable output is never styled

Anything a caller parses goes through a helper in `commands/_common.py`, never through
`pp.console().print_json()` or a bare `pp.console().print()`:

- `emit_json(payload)` for a `--json` payload
- `emit_value(text)` for a single value or block a caller consumes

Rich colors JSON and highlights values by default, and it honors `FORCE_COLOR`, which
real shells export, so styling does not depend on stdout being a terminal. One escape
sequence turns a payload into a parse error and a path into something `cd "$(...)"`
cannot use. The helpers pass `markup=False`, `emoji=False`, `highlight=False`, and
`soft_wrap=True`. `emoji=False` is separate from `markup=False` because Rich replaces a
`:word:` segment with the named emoji independently of markup, so a vault path containing
one would print as a path that does not exist. `soft_wrap=True` keeps a long path from
being wrapped mid-value.

Prose for a person keeps its styling: `pp.success`, `pp.warning`, and `pp.error`.

Every `--json` payload goes through `emit_json`, and every prose output a caller parses
goes through `emit_value`. Four are worth naming. `search` prints a path, a title, and a
summary per hit, all of which a caller copies or parses, and bracketed prose in a summary
is exactly what Rich would eat as markup. `inject` prints a block a caller redirects or
pipes whole. `new --json` prints the path a caller writes the body into, and
`project --json` prints every path the plugin's hooks read.

`CliRunner` is not a terminal and the console decides its color system at import, so an
in-process test proves nothing here. `tests/integration/test_cli_machine_output.py` runs
the CLI in a subprocess with color forced in the environment and asserts the raw bytes
carry no `\x1b[`.

### Adding a New Command

1. Write the logic as a library function in `src/sessionmemory/lib/`, with no Typer
   imports.
2. Add a function to a module in `src/sessionmemory/commands/`, either an existing one
   or a new one, that parses options, calls the library, and reports through `pp`. A
   command that reads or writes an index builds its embedder through
   `_common.build_embedder()` and calls `lib/fieldindex` with it, so the stub embedder
   stays selectable from the environment.
3. Register it in `cli.py` with `app.command("<name>", help="...")(module.function)`.
   Reach for `app.add_typer()` only if the command takes sub-commands.
4. Add tests: `tests/unit/` for the library function, `tests/integration/` for the
   command as invoked through `sessionmemory.cli.app`.

### The memoryfield format

The format is Cal Paterson's. The spec is
<https://github.com/calpaterson/memoryfield-spec/blob/main/SPEC.md>, the article behind it
is <https://calpaterson.com/memoryfields.html>, and every "the spec" below means that
document. His reference implementation is
<https://github.com/calpaterson/memoryfield-tool>, and it is the independent check on this
one: it can `connect` to any field directory here as is, and its `validate` passes every
field in the vault. Its default search cutoff for this model, 0.45, is the same number
measured here. Never let it write an index file ours reads: it embeds through Ollama, and
the vectors are not guaranteed to match fastembed's. It also pins `pysqlite3-binary`,
which has no Apple Silicon wheel, so on a Mac it runs only with that dependency dropped
from a scratch copy.

A project's `learnings/` and `logs/` are fields: flat directories of pages, each
with its own index file. `specs/`, `plans/`, and `backlog.md` sit beside them and are
never indexed, which is what keeps a spec or a checklist from being embedded as memory.
There is no global scope and no cross-project read; `lib/paths.py` finds a project's
files by its slug and nothing else.

**`lib/field.py` is the one place a page's shape is known.** The filename rule, the
debris rule, and the 8KB limit all come from the memoryfield spec and live there.
`iter_pages` is the only definition of what counts as a page: a conformant name at the
top level of the field, since the spec forbids indexing a page in a sub-directory. A
file with any other name is reported by `doctor` and read by nothing. `is_debris` drops
`.DS_Store`, `desktop.ini`, `Thumbs.db`, a Syncthing conflict copy, and an editor backup
ending in `~`, because the spec requires ignoring them rather than reporting them.

A page has five frontmatter fields and no more. `read_page` never refuses a page without
frontmatter, since the spec makes that a valid page, and it decodes with
`errors="replace"` so one corrupt byte cannot crash a read of the whole field.

`PAGE_LIMIT` is 8192 bytes, and it is a limit on the page rather than on the file
`fieldindex` reads. `embedding_input` truncates to the first 8192 bytes without splitting
a character, so an oversized page is embedded in part rather than skipped or chunked.
Chunking is what the limit exists to avoid: one page is one embedding, and the remedy for
more detail is a second page. `doctor` reports the oversized page, and the truncation is
what keeps that finding a suggestion rather than a failure.

`write_page` is the single seam every page write goes through, and it exists because of
markdown formatters. It skips the write outright
when the file on disk already parses to the same metadata and body, because a markdown
formatter rewrites frontmatter quoting and sequence indentation into a shape
`frontmatter.serialize` does not produce. Comparing parsed content rather than rendered
bytes is what makes a difference of formatting alone read as no change. Anything that
grows a second way to write a page has to keep that property, so add it to `write_page`
rather than beside it.

`claim_filename` takes a name by exclusive creation rather than by checking first, so two
writers racing for one title get two files rather than one overwritten page.

### The index

One SQLite file per field, named `<embedder.name>.sqlite3` by `fieldindex.index_path`, so
two embedders never share an index and changing the model needs no migration. It holds
the spec's table and nothing else:

```sql
CREATE TABLE pages (
    filename      TEXT PRIMARY KEY,
    frontmatter   JSON NOT NULL,
    last_modified DATETIME NOT NULL,
    sha256_hash   BLOB NOT NULL,
    embedding     BLOB NOT NULL
);
```

The embedding input is the whole file, frontmatter included, as the spec requires.
Freshness is the sha256 of the raw bytes, so a page edited in any editor is re-embedded
on the next read and nothing has to be told that a file changed. `search` calls the same
`_refresh` before it queries, which is why a page written moments ago is found without a
`reindex`. `reindex` exists to do that work up front, not because anything depends on it
having been run.

The index is derived, gitignored, and never backed up. Deleting `<field>/*.sqlite3` and
running `sessionmemory reindex` reproduces it exactly, and the restore procedure for a
whole vault is `git clone` followed by `sessionmemory reindex`. `fieldindex._open` treats
a corrupt file the same way: it deletes the file and its `-journal` sibling and reconnects
once, rather than letting a damaged cache crash a read that has no reason to fail. Nothing
may treat a value read from the index as authoritative.

**There is no application lock, and nothing here needs one.** A page write is an atomic
rename, each field's index is one SQLite file under SQLite's own locking with a five
second busy timeout, and two sessions ending in different projects touch different files
entirely. Two hooks committing at once race on git's own `index.lock`, the loser skips,
and the next session's hook commits what it left behind. Do not reintroduce
`lib/locking.py`: the shared index and the taxonomy file it guarded are both gone.

Two environment variables exist for tests rather than for a person configuring the CLI:

- `SESSIONMEMORY_EMBEDDER=stub` selects the deterministic, hash-derived `StubEmbedder`
  instead of the real model. `tests/conftest.py` sets it for the whole suite with an
  autouse fixture, since loading the real model costs seconds and, on a machine with no
  cached copy, downloads 520MB.
- `SESSIONMEMORY_MODEL_CACHE` overrides where the real model is cached, in place of
  `embed.DEFAULT_CACHE`.

`_common.build_embedder` reads the first at call time rather than at import, so a test
selects the stub with `monkeypatch.setenv`.

### The embedder

`lib/embed.py` runs `nomic-embed-text-v1.5` on ONNX Runtime in this process through
`fastembed`. Ollama was rejected for one reason: it is a service, and a service is
something every write would depend on being up. A model file on disk cannot be down.

`MODEL_CODE` and `MODEL_NAME` are two different strings and neither substitutes for the
other. `MODEL_CODE` is `nomic-embed-text-v1.5`, the model code the spec names and the
index file is named for. `MODEL_NAME` is `nomic-ai/nomic-embed-text-v1.5`, the repository
id fastembed loads. Changing the model changes `MODEL_CODE`, which changes the index
filename, which is the whole migration: the old file is orphaned and the new one is built
on first use.

The model is trained with task prefixes and fastembed 0.8 does not add them, so
`DOCUMENT_PREFIX` and `QUERY_PREFIX` are prepended here. The spec permits that for a
model that mandates them. Both prefixes stay in this file so that a stored page and a
query are never embedded under different conventions, which would degrade ranking without
failing anything.

`DEFAULT_CACHE` is pinned to `~/.cache/sessionmemory/models` rather than left at
fastembed's default under the system temp directory. macOS purges that periodically, and
a silent re-download of 520MB in the middle of a command is indistinguishable from a
hang.

### Injection

`sessionmemory inject` emits a fixed guidance block, then every learning's title, then a
count of open backlog items with the titles of any specs and plans. **The index is never
read here**, and no page body ever enters an injection, which is what makes its cost
proportional to the number of pages rather than to their length. `lib/inject.build` reads
the files directly.

Titles rather than summaries, because injection is a push channel and must not grow into
the thing it is trying to save. Titles rather than nothing, because pushing titles trades
recall for recognition: an agent that hits a failing test starts debugging and never
generates the idea of searching, but it can notice that a title matches its situation,
and models are far better at the second than the first.

The guidance exists because the block under it is data. It says what this project knows
and nothing about what to do with it, so a session receiving only titles learns the vault
exists and not how to read or write it. It cannot be left to a skill: three of the four
ship `disable-model-invocation: true` and are the user's slash commands, and the one that
is model-invocable only reaches an agent that happens to match its description, which
fails silently. `--command` is how the guidance spells this CLI, defaulting to
`sessionmemory` for a human and set to the shim's absolute path by `VaultCLI.inject`,
since a session that reached the plugin without installing the CLI as a tool has no
`sessionmemory` on `PATH`.

Injection is deliberately unranked and unlimited. If the titles prove not to be read, the
next thing to try is a `UserPromptSubmit` hook that embeds each prompt and injects the
nearest pages above a threshold, ahead of any ranking of the titles themselves. The
threshold is already measured: `fieldindex.DEFAULT_MAX_DISTANCE` is 0.45, and `search`
returns nothing beyond it rather than the nearest pages dressed up as hits. `--read`
prints every hit's whole file, so one call replaces a search and a Read per hit.

### The Plugin Half of This Repository

This repository is also a Claude Code plugin. `.claude-plugin/marketplace.json` declares
one marketplace holding one plugin, `sessionmemory`, sourced from `./`. A local-path
marketplace installs by copying this repository into
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, so `${CLAUDE_PLUGIN_ROOT}`
inside any hook is that copy, never this checkout. `bin/sessionmemory` inside the copy
resolves `ROOT` to the copy's own directory and runs `uv run --project` there, against an
environment that belongs to the copy alone; the first run after an install pays to build
it. A commit to this repository reaches a running session only after
`/plugin update sessionmemory@sessionmemory` refreshes the copy, or a reinstall replaces
it.

`bin/sessionmemory` is the only thing in this repository that knows the CLI runs through
`uv`. It resolves its own location by walking any symlink chain back to the directory it
lives in before running `uv run --project "$ROOT" --frozen vault "$@"`, so a shim reached
through a link on `PATH` still finds the project it belongs to. Every hook, skill, and
agent reaches the CLI by executing this path; nothing else may invoke `uv` directly, which
is what keeps the invocation strategy changeable in one place without touching every
caller.

`hooks/` imports nothing from `src/`. The hooks are standalone `uv run --script` programs
with no dependencies of their own, and they reach the CLI only by executing
`bin/sessionmemory`, the same as a human would from a shell. A hook that imported
`sessionmemory` directly would pay for the CLI's whole dependency graph just to decide
whether to shell out to it.

There is deliberately no schema-version check between the two halves. It existed to catch
the plugin and the CLI drifting apart across two repositories that shipped and versioned
separately. There is now one checkout and one version of both, so a checkout cannot drift
from itself, and adding the check back would be solving a problem this repository no
longer has.

`sessionmemory log` replaces a note's body on every call, and `--body` defaults to empty,
so any caller has to send the complete body every time, never a diff or an addendum. This
is why the sweep hands the model the note's existing log before asking it to compose the
new one: without it, the model has no way to keep what was already recorded.

The sweep may write anywhere inside its own project's folder, which is what a later pass
strengthening a memory means. Two backstops run after the model finishes, and neither is
complete. `Sweep._validate_writes` walks the tool-use stream's reported paths and reverts
anything outside the project folder, restoring it from the vault's `HEAD` or quarantining
it when git cannot. It then scrubs for secrets by mtime: every file under the project
folder modified since the run started, which is what catches a write made through Bash,
since a Bash write never appears in the tool-use stream. A Bash write **outside** the
project folder, a sibling project's folder foremost, is caught by nothing here and is
committed by the next hook. Git history is the recovery. The prompt forbids deleting a
page, and a page the model deletes through Bash is committed as a deletion.

`hooks.json` budgets each hook's own worst case. `SessionStart` runs `Store.for_cwd` (one
git call, `_GIT_TIMEOUT` 5s), `head_commit` (5s), the vault commit (`COMMIT_GIT_TIMEOUT`
5s across up to seven git calls, so 35s), `VaultCLI.inject` (`TIMEOUT` 25s), and
`VaultCLI.registered` (`RESOLVE_TIMEOUT` 5s): a worst case of 75s under a timeout of 90.
`SessionEnd` gates inline and then commits, so its 60 covers the same 35s with headroom.
`PreCompact` only gates and spawns, and its timeout is 10. Raising any of those constants
means raising the timeout that covers them.

Both start and end hooks skip their commit while a sweep worker holds a fresh lock, since
the worker commits its own writes when it finishes. `sessionend.py` commits whether or not
the sweep is enabled, so a session that wrote a page by hand still lands it.

### Committing

`hooks/sessionhooks/commit.py` is what replaced the scheduled checkpoint job. `SessionStart`
commits before it injects, `SessionEnd` commits after it triggers the sweep, and the sweep
worker commits once its own writes are validated, so a page reaches git inside the session
that produced it.

Every git subprocess runs under `store.git_safe_env`, which drops every `GIT_`-prefixed
variable. Git exports `GIT_DIR` and `GIT_INDEX_FILE` into anything it runs, so a hook
launched from inside another git operation would otherwise retarget `git add -A` at
whatever repository set those variables rather than at the vault. The rule is a prefix
rather than a fixed set because the `GIT_CONFIG_KEY_<n>` family is indexed and cannot be
enumerated, and a config override such as `core.worktree` relocates a repository exactly
as `GIT_DIR` does.

Staging is `git add -A -- :/ ':(exclude,glob)**/*.sqlite3*'`. `:/` keeps the whole
repository in scope for a vault nested inside a larger one, and the exclusion keeps the
derived index out of history whatever the vault's `.gitignore` says, since
`sessionmemory init --force` adopts a directory without replacing an ignore file that
predates the index. `_commit` checks `git add`'s exit status: `git add` stages what it can
reach and still exits non-zero on a path it could not read, and committing over that
writes a commit silently missing the one file that needed attention.

A commit is skipped rather than forced in five states: not a repository, a clean tree, a
merge or rebase or cherry-pick or revert in progress, a detached `HEAD`, and a lost race
on git's `index.lock`. None of them is an error worth surfacing from a hook. The next
session's hook commits whatever was left behind, which is what makes skipping cheap and
retrying automatic.

### Doctor

`lib/doctor.py` holds six checks and the `CHECKS` tuple that runs them. There are no
tiers: every finding is a suggestion, `doctor_command` always exits 0, and nothing here
can fail a build. Semantic retrieval never surfaces a page that does not match, so an
imperfect page costs nothing, and a check that failed a build over one would fail it
forever.

The six are a nonconformant filename, a page over `PAGE_LIMIT`, frontmatter that cannot
be parsed or a learnings page missing `title` or `summary`, a bare date or datetime in
frontmatter, a registered project whose root no longer exists, and an index that is
unreadable or behind its pages. A file that is not valid UTF-8 is reported by the
frontmatter check. A logs page with no frontmatter block at all is fine, since a log has
nothing a search result would show. The datetime check has its own reader,
`frontmatter.unquoted_datetime_keys`, because `frontmatter.parse` stringifies a date
PyYAML typed and nothing downstream can then tell a quoted value from a bare one.

**There is no `--fix`, and repair is not coming back.** Every finding above forks on a
judgment the command cannot make: which name to rename a file to, where to split a page,
what a missing summary should say, whether a repository moved or died. The previous
version of this project grew a 294-line repair module by adding one plausible case at a
time, and a repair that guesses destroys the signal that made the finding worth reporting.

### Testing

- `tests/unit/` for the library: page read and write, frontmatter round-trip, filename
  conformance, index build and search.
- `tests/integration/` for the CLI as invoked through `sessionmemory.cli.app`.
- `tests/plugin/` for the hooks, which are exercised as subprocesses.
- `tests/integration/test_conformance.py` asserts that a built field satisfies the
  memoryfield spec's requirements: a flat directory, the filename rule, an index named for
  the model code, and the whole file as the embedding input.
- `tests/integration/test_cli_machine_output.py` runs the CLI in a subprocess with color
  forced and asserts that nothing a caller parses carries an escape sequence.

### User-Facing Documentation

`README.md` and `docs/` are written for a stranger on GitHub who has never run this, and
this file is written for whoever changes the code. They are not two views of one text. The
README leads with what a person gets and never with the CLI, which is plumbing in that
frame however central it is here.

Four pages sit under `docs/`, and a change to behavior belongs in whichever ones cover it:

- `concepts.md`: pages, fields, the layout, the index, what a session receives, and the
  format's conformance and deviations.
- `cli.md`: every command, its options, and its real output.
- `plugin.md`: the hooks, the sweep, every setting key and default, the slash commands,
  the review subagents.
- `vault-health.md`: the six `doctor` checks and what to do about each.

A new command, a new option, a new `doctor` check, a new settings key, or a changed
default is a documentation change as much as a code change. The output in those pages is
copied from real runs rather than composed, so re-run the command and paste what it
prints. The runs come from a scratch vault holding one registered project, `invoice-api`,
and the pasted output shortens the home directory to `~`, so a path reads
`~/repos/my-vault/projects/invoice-api/...`. Nothing in the documentation names a real
machine, user, repository, or page: the project is public, and a reader's own paths are
the only ones that matter.

## Conventions

- Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Docstrings: Google format
- Type hints on all function signatures
- `ty` for type checking
- `ruff` for linting and formatting
- `prek` replaces `pre-commit` for pre-commit hooks
