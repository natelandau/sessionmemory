# CLAUDE.md

## Overview

This project keeps a coding agent's durable memory as markdown. Each registered project
owns one folder in a vault, and inside it `learnings/` and `logs/` are fields: flat
directories of pages, each with its own deletable vector index. The CLI creates pages and
searches them by meaning, and does nothing an agent already does well with Read, Grep,
and Write. The repository is also a Claude Code plugin (`hooks/`, `skills/`).

User-facing behavior is documented in `README.md` and `docs/` (`concepts.md`, `cli.md`,
`plugin.md`, `vault-health.md`). Read those for what a command or hook does; this file
covers what to know before changing the code. Most modules carry a docstring explaining
their own design, so read the module before reasoning about it.

## Running

Every command finds the vault through `SESSIONMEMORY_VAULT`, falling back to `vault.root`
in `~/.claude/sessionmemory.toml` (`lib/config.vault_root`). The variable wins, so point it
at a throwaway directory when running by hand:

```bash
export SESSIONMEMORY_VAULT=/tmp/scratch-vault && mkdir -p "$SESSIONMEMORY_VAULT"
uv run sessionmemory init
uv run sessionmemory --help
```

`init` is a human bootstrap command. An agent never runs it, and no error message may
suggest `init --force` as a next action.

## Development

```bash
uv sync                  # Install dependencies
uv run duty lint         # All linters (ruff, ty, typos, yamllint, shellcheck, prek)
uv run duty test         # Tests with coverage
```

Every check duty runs through `gated()`, which wraps it in `gate-cache`. A check that
already passed on the same git tree prints `already passed ... skipping` and exits 0;
that is a pass. `GATE_CACHE_DISABLE=1` forces a real run. A new check needs a duty that
calls `gated()`, a local prek hook running `uv run duty <name>`, and its hook id in
`PREK_DUTY_HOOKS`. An input git does not track goes in the gate name, as the test duty
does with the Python version.

## Code Layout

- `src/sessionmemory/entry.py`: the console script. It answers a bare `--version` without
  importing the commands, and hands everything else to `cli.py`, which registers them.
- `src/sessionmemory/commands/`: Typer commands. They parse options, call the library,
  and report through `pp`. They know nothing about file formats.
- `src/sessionmemory/lib/`: the logic, with no Typer imports.
- `hooks/`: standalone `uv run --script` programs. They import nothing from `src/` and
  reach the CLI only by executing it.
- `tests/unit/` (library), `tests/integration/` (CLI through `sessionmemory.cli.app`),
  `tests/plugin/` (hooks, run as subprocesses).

### Adding a command

1. Library function in `lib/`.
2. Command function in `commands/`. A command that touches an index builds its embedder
   with `_common.build_embedder()` so the test stub stays selectable, and `--json` is
   `_common.JSON_OPTION`.
3. Register in `cli.py` with `app.command("<name>", help="...")(module.function)`. Use a
   group (`app.add_typer`, own `typer.Typer(cls=VersionedGroup, help=...)`, as `new.py`
   does) only when the command has sub-commands.
4. Unit and integration tests.

A command's docstring renders as its `--help`, including any `Args:` or `Raises:`
section. When ruff requires such a section, pass an explicit `help=` at registration.

### Commands, flags, and names

- A command is a verb: one action with its own object, side effects, and failures. A flag
  changes how its command does that job: what it targets, its scope, its output, or which
  side wins a choice the command already faces. If a flag makes the command do something
  it never does without it, that is a command. `local adopt` is a command because `sync`
  never moves files into the vault on its own; `--keep-settings` is a flag because `sync`
  already finds the two settings copies apart and the flag picks the winner.
- A repeatable flag that takes the things acted on (`--x A --x B`) is a command's
  arguments in disguise.
- Every name, on the CLI surface and in the code (commands, options, JSON keys,
  functions, classes, variables), says what it is or does without the reader opening it,
  and admits one reading. A name that reads as both a noun and a verb, names a mechanism
  instead of its effect, or could mean a neighbor's job gets renamed.

### Console output

- All output goes through `nclutils.pp` (load the `nclutils` skill). Never `print()` or
  instantiate `rich.Console()`.
- Anything a caller parses goes through `_common.emit_json` or `_common.emit_value`,
  never `pp.console().print*`. Rich styles output even off a terminal (`FORCE_COLOR`),
  and one escape sequence breaks a payload or a `cd "$(...)"`. This covers every `--json`
  payload, `search`, `inject`, the path every `new` command and `log` print, `path`,
  and the `local` subcommands.
- `tests/integration/test_cli_machine_output.py` checks this in a subprocess with color
  forced. `CliRunner` cannot, since the console picks its color system at import.

## Invariants

### Pages and fields

The format is Cal Paterson's memoryfield spec
(<https://github.com/calpaterson/memoryfield-spec/blob/main/SPEC.md>). His reference
tool (<https://github.com/calpaterson/memoryfield-tool>) can validate our fields, but
never let it write an index ours reads: its Ollama vectors need not match fastembed's.

- `lib/field.py` is the one place a page's shape is known: filename rule, debris rule,
  8KB `PAGE_LIMIT`, and `iter_pages` as the only definition of a page.
- `write_page` is the single seam for page writes. It skips writes whose parsed content
  is unchanged so a markdown formatter's reshaping reads as no change. A second write path
  goes into `write_page`, not beside it.
- An oversized page is embedded truncated, never chunked. One page, one embedding.
- `lib/backlog.py` owns the backlog item shape. `new backlog` is the CLI's only write to
  `backlog.md`, and it only appends.
- `lib/localmd.py` owns local entries (`CLAUDE.local.md`, `skills/<n>`, `rules/<n>`, any
  path adopted by name, `settings.local.json`) and their states. A path adopted by name
  has no fixed depth, so `lib/localmanifest.py` lists it in `local/entries.toml`; only
  adopt writes that file, and the other kinds are never listed there. Every entry but the
  settings file is linked; that one is copied, only in the direction the person picks, and
  is never part of `entries()`, so no link operation reaches it. `lib/localtree.py` holds
  the tree copy, compare, and link placement it uses, and `lib/settingsdiff.py` the
  settings summary. Everything an entry keeps in the vault lives under
  `projects/<slug>/local/`.
- `lib/localdeclined.py` owns `local/declined.toml`, the entries a person declined to
  adopt, which `candidates()` never offers. Only `local decline` and a no in `local sync`
  add to it, and only adopt removes a name. Nothing prunes a name the current checkout
  lacks, since another checkout may hold it.
- `specs/` is indexed but is not a field. `plans/`, `runbooks/`, `reference/`, `local/`,
  and `backlog.md` are never indexed. There is no cross-project read.

### The index

- One SQLite file per field (and one in `specs/`), named for the embedder, holding only
  the spec's `pages` table. Changing the model changes `embed.MODEL_CODE`, which renames
  the file; that is the whole migration.
- The index is derived and disposable. Nothing may treat a value read from it as
  authoritative. Freshness is the sha256 of raw bytes, and `search` refreshes before
  querying, so nothing depends on `reindex` having run.
- Bump `specs.INPUT_VERSION` whenever `specs.embedding_input` changes what it derives,
  since the hash cannot see that change.
- Search thresholds (`DEFAULT_MAX_DISTANCE`, `DEFAULT_MIN_MARGIN`, the `SPECS_*` pair,
  `MIN_BACKGROUND_PAGES`, `FALLBACK_BACKGROUND`) came from labeled queries against real
  vaults. Change them only by re-measuring, never by reasoning about one query. The
  vault's specs hold the measurements.
- `DOCUMENT_PREFIX` and `QUERY_PREFIX` stay together in `lib/embed.py` so pages and
  queries are never embedded under different conventions.

### Locking

There is no general application lock; do not reintroduce `lib/locking.py`. The CLI's one
lock is `registry.locked`, held around every load-modify-save of `registry.toml`, and
`registry.save` is called only inside it. The plugin's one lock is the per-project
`sweep.lock` flock held by the sweep worker; that file is never deleted.

### Injection

`lib/inject.build` never reads the index and never includes a page body, so its cost
scales with page count, not page length. The guidance block's text is the only place a
session learns how to use the vault, so a CLI change that alters a command it names
must update it.

## The Plugin

- Installing copies this repo into `~/.claude/plugins/cache/...`, so `${CLAUDE_PLUGIN_ROOT}`
  is that copy. A commit reaches a session only after `/plugin update sessionmemory@sessionmemory`.
- `VaultCLI.discover` (`hooks/sessionhooks/vaultcli.py`) is the one place that picks the
  CLI: the `sessionmemory` on `PATH` if its version is at or past the plugin's, else the
  `bin/sessionmemory` shim. Skills resolve it via `hooks/vault-path.py --cli`.
- `bin/sessionmemory` is the only thing allowed to invoke `uv`.
- `.claude-plugin/plugin.json` and `pyproject.toml` share a version, bumped together by
  commitizen `version_files`.
- `sessionmemory log` replaces the whole body on every call, which is why the sweep is
  handed the existing log first.
- `sweep.md` is the one statement of the sweep's rules for both runs; `<!-- headless -->`
  and `<!-- manual -->` blocks are kept for one kind and dropped for the other.
- `hookmain.begin` opens every hook and configures logging. Log one INFO line per
  decision, and every fail-open `except` logs at ERROR before swallowing.
- Every git subprocess runs under `store.git_safe_env`. Commit staging and its skip
  reasons are documented in `hooks/sessionhooks/commit.py`.
- Sweep backstops (containment, secret scrub, shape checks, protection of every file
  under `local/`) live on `WriteGuard` in `hooks/sessionhooks/guard.py`. The secret scrub
  skips `local/`, since the local guard restores those files byte for byte.

### Hook timeouts

`hooks.json` budgets each hook's worst case from the timeout constants it calls, and
`tests/plugin/test_hooks_manifest.py` asserts the `SessionStart` timeout exceeds their
sum. Raising any such constant means raising the timeout that covers it.

`SessionEnd` is different: Claude Code gives all SessionEnd hooks a shared 1.5s budget
that a plugin cannot raise. Nothing it runs inline may start the vault CLI. It finds the
vault with `VaultCLI.locate`, commits with git alone, and leaves the version handshake to
the detached sweep worker.

## Testing

`tests/conftest.py` sets `SESSIONMEMORY_EMBEDDER=stub` for the whole suite, since the
real model costs seconds and may download 520MB. `SESSIONMEMORY_MODEL_CACHE` overrides
the model cache location.

## Documentation

`README.md` and `docs/` are for a stranger on GitHub. The README leads with what a person
gets, not with the CLI. A new command, option, `doctor` check, settings key, or changed
default is a documentation change too. Output in `docs/` is pasted from real runs against
a scratch vault holding one project, `invoice-api`, with the home directory shortened to
`~`. Nothing in the docs names a real machine, user, repository, or page.

## Conventions

- Google-style docstrings, type hints on every signature
- `ruff` for lint and format, `ty` for types, `prek` for pre-commit hooks
