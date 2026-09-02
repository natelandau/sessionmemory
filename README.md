# sessionmemory

Durable memory for coding agents, one folder of searchable pages per project.

An agent starts every session knowing nothing about the last one. Yesterday's session
worked around a trap, rejected the obvious approach for a reason, and found the one flag
that makes a library behave. All of it ends with the session, and the next one works it
out again from nothing. `sessionmemory` keeps what a project learned and hands it back
when the next session starts. Each project keeps its own memory, and no page is ever
shared between projects.

| Part                       | What it is                                                                     |
| -------------------------- | ------------------------------------------------------------------------------ |
| The vault                  | Your knowledge as markdown pages, one folder per project                       |
| The `sessionmemory` plugin | Claude Code hooks that feed a session at its start and record it at its end    |
| The `sessionmemory` CLI    | Searches pages by meaning and creates them. Everything else is a file you edit |

The pages follow the memoryfield format by Cal Paterson, described in
[his article](https://calpaterson.com/memoryfields.html) and defined in
[the memoryfield spec](https://github.com/calpaterson/memoryfield-spec). Each project's
`learnings/` folder is one field in that format: a flat directory of markdown pages beside one
vector index file. You can export that folder, share it, or read it with any tool that
speaks the format.

## Requirements

- Python 3.13 or 3.14
- [uv](https://docs.astral.sh/uv/)
- git. The vault is a git repository, and a project is registered by its git remote.
- Claude Code, to run the plugin. The CLI works on its own without it.

The first search downloads the `nomic-embed-text-v1.5` embedding model, about 520MB, and
caches it under `~/.cache/sessionmemory/models`. Nothing else touches the network.

## Install the CLI

Clone the repository and install its dependencies. Keep the clone: it is the source the
plugin installs from, and it is where you pull updates.

```bash
git clone https://github.com/natelandau/sessionmemory ~/repos/sessionmemory
cd ~/repos/sessionmemory
uv sync
```

To put a `sessionmemory` command on your `PATH`, install the package as a tool:

```bash
uv tool install .
```

Without that step, run the CLI as `uv run sessionmemory` from the clone, or through the
`bin/sessionmemory` shim, which works from any directory.

## Create a vault

The vault is its own directory. Make it a git repository of its own, so your pages and
this code do not share a history.

```bash
mkdir -p ~/repos/my-vault
cd ~/repos/my-vault
git init
export SESSIONMEMORY_VAULT=~/repos/my-vault
sessionmemory init
```

Put the `export` in your shell profile, and give it an absolute path. Every directory the
CLI prints is built from that value.

`sessionmemory init` writes the three files a vault needs. A marker in `_system/vault.toml`
identifies the directory as a vault. A `.gitignore` keeps the derived index out of your
history. A README explains the layout to whoever opens the vault later. `sessionmemory init`
never overwrites a file, so you can run it again safely.

Until a directory holds that marker, every command refuses to touch it. The refusal
protects you. If `SESSIONMEMORY_VAULT` points at your home directory by mistake, the
first page written scatters a `projects/` tree into it.

Nothing commits the vault on a timer. The plugin commits it when a session starts and
again when a session ends, so a page reaches git within the session that wrote it.
Pushing that history to a remote stays yours to do.

> **Note:** To bring an existing directory of notes under the CLI, run
> `sessionmemory init --force ~/repos/my-vault` once. `--force` means only that the
> directory already has contents. Nothing existing is overwritten.

## Register a project

A project gets memory when its repository is registered. Registration is the only
decision, and it happens once, from inside the repository:

```bash
cd ~/repos/invoice-api
sessionmemory project --register --cwd .
```

```
✓ registered 'invoice-api'
  └─ root: ~/repos/invoice-api
```

`--register` reads the git remote and the repository root, and derives the slug from
them. There is nothing else to choose: no tags, no scope, no note type. The slug is
permanent once pages carry it, so an unregistered directory is told to run this command
rather than registered for you.

## Search and write pages

The CLI does two things. It finds pages by meaning, and it creates pages. Reading and
editing a page is a job for your editor or your agent's own tools.

```bash
sessionmemory search "why does the same stripe event arrive twice" --limit 2 --cwd .
```

```
~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
  Stripe retries a webhook for 72 hours, so the handler must be idempotent
  Stripe redelivers an unacknowledged webhook for up to 72 hours, so the handler records the event id and ignores a repeat.

~/repos/my-vault/projects/invoice-api/learnings/the-nightly-reconciliation-job-must-start-after-the-02-00-bank-feed.md
  The nightly reconciliation job must start after the 02:00 bank feed
  The bank feed lands at 02:00 UTC; a reconciliation run before it reports every open invoice as unpaid.
```

A result is a path, a title, and a summary. A paraphrase finds the page, because search
ranks by meaning and not by words in common. A query that nothing answers returns no
results rather than the nearest pages. Pass `--read` to print every hit in full.

```bash
sessionmemory new learning \
  --title "Stripe retries a webhook for 72 hours, so the handler must be idempotent" \
  --summary "Stripe redelivers an unacknowledged webhook for up to 72 hours, so the handler records the event id and ignores a repeat." \
  --cwd .
```

```
✓ created stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
  └─ ~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
```

The vault path is shortened to `~` here; the command prints absolute paths.

The command writes the frontmatter and prints the path. Write the body into that file,
or pass it with `--body-file`. The title is what every future session sees at its start,
and the summary is what a search result shows. Both state the fact and not the topic.

## Install the Claude Code plugin

Add the clone as a marketplace, then install the plugin from it:

```
/plugin marketplace add ~/repos/sessionmemory
/plugin install sessionmemory@sessionmemory
```

The GitHub shorthand `natelandau/sessionmemory` works as a marketplace source too.
Either way, Claude Code copies the plugin into its own cache and runs the hooks from that
copy, not from your clone. After you pull changes into the clone, run
`/plugin update sessionmemory@sessionmemory` to refresh the copy.

A hook does not start from an interactive shell. A session launched from a GUI or an IDE
cannot see a root exported in `.zshrc`, so record the vault root in
`~/.claude/sessionmemory.toml` as well:

```toml
[vault]
root = "~/repos/my-vault"
```

From then on, a session that starts in a registered repository receives that project's
memory. A session that ends or compacts hands its transcript to a background pass, which
records what was worth keeping.

## What a session sees

`sessionmemory inject` prints the block a session starts with. This is the block for a
project holding four learnings, one spec, one plan, and two open backlog items:

```
## Using this vault

Durable memory for this project lives in a vault of markdown pages. Below is the
list of what it already knows; each title is one `sessionmemory search` away.

  - A title below matches what you are doing: `sessionmemory search "<words>"` returns
    the page's path, and you Read it. Search before assuming nothing was written down.
  - Past sessions: `sessionmemory search "<words>" --logs`. Open work: read `backlog.md`
    in the project's vault folder (`sessionmemory project --json` prints every path).
  - Something worth keeping past this session: `sessionmemory new learning --title "..."
    --summary "..." --cwd .` creates the page and prints the path to write prose into.
    Keep a page under 8KB; more detail is another page.
  - Specs and plans: `sessionmemory new spec|plan --title "..." --cwd .` creates the file.
    Edit `backlog.md`, specs, and plans directly; the CLI only creates pages.

## What this project knows

  - Invoice numbers come from a Postgres sequence, never from max(id) plus one
  - pytest-asyncio needs asyncio_mode = auto or every async test is skipped
  - Stripe retries a webhook for 72 hours, so the handler must be idempotent
  - The nightly reconciliation job must start after the 02:00 bank feed

## Open work

  2 open backlog items
  spec: Export invoices as UBL 2.1 XML
  plan: Move PDF rendering to a worker queue
```

A page body never enters that block, so its cost grows with the number of pages and not
with their length. The titles say what exists. `sessionmemory search` returns what they say.

## Documentation

| Page                                     | What it covers                                               |
| ---------------------------------------- | ------------------------------------------------------------ |
| [Concepts](docs/concepts.md)             | Pages, fields, the index, and the layout of a vault          |
| [CLI reference](docs/cli.md)             | Every command, its options, and its output                   |
| [The Claude Code plugin](docs/plugin.md) | The hooks, the sweep, every setting, and the slash commands  |
| [Vault health](docs/vault-health.md)     | What `sessionmemory doctor` reports, and what to do about it |

## Development

```bash
uv sync                  # install dependencies
uv run duty lint         # ruff, ty, typos, prek
uv run duty test         # pytest with coverage
```

`CLAUDE.md` records the conventions this project holds itself to.

## License

MIT. See [LICENSE](LICENSE).
