# CLI reference

Every command below runs as `sessionmemory <command>`. From the clone without a tool
install, run `uv run sessionmemory <command>` or `bin/sessionmemory <command>` instead.

## Conventions

Every command reads `SESSIONMEMORY_VAULT` to find the vault. With the variable unset, or
pointing at a directory that `sessionmemory init` never touched, the command exits 1 and
says so:

```
✗ SESSIONMEMORY_VAULT is not set. Point it at your vault repository.
  └─ export SESSIONMEMORY_VAULT=/path/to/your/vault
```

Every command except `init`, `delete`, and `doctor` resolves a project from the working
directory, so each one accepts `--cwd` to name a different directory. A directory that no
project owns exits 1:

```
✗ this directory is not a registered project
  └─ run: sessionmemory project --register
```

`sessionmemory project` prints the same refusal with the directory named, since reporting
on a directory is its whole job.

```
✗ ~/repos/invoice-api is not a registered project
  └─ run: sessionmemory project --register
```

| Flag              | What it does                                                       |
| ----------------- | ------------------------------------------------------------------ |
| `--json`          | Print a machine-readable payload instead of prose. Never styled.    |
| `--cwd <path>`    | Resolve the project from that directory instead of the shell's.     |
| `-v`, `-vv`       | Increase output verbosity. Repeat for more.                         |
| `--body-file <p>` | Read a markdown body from a file, or from stdin for `-`.            |

The output below comes from real runs against a vault at `~/repos/my-vault` holding one
registered project, `invoice-api`, checked out at `~/repos/invoice-api`. The CLI prints
absolute paths; the home directory is shortened to `~` here.

## `sessionmemory init`

Create the files a new vault needs.

| Option        | What it does                                                             |
| ------------- | ------------------------------------------------------------------------ |
| `[directory]` | Where to create the vault. Defaults to `SESSIONMEMORY_VAULT`.            |
| `--force`     | Initialize a directory that already has contents. Nothing is overwritten. |
| `--json`      | Emit JSON instead of prose.                                              |

```bash
sessionmemory init                       # in SESSIONMEMORY_VAULT
sessionmemory init ~/repos/my-vault      # in a named directory
sessionmemory init --force ~/old-notes   # a directory that already has contents
```

```
✓ initialized ~/repos/my-vault
  ├─ .gitignore
  ├─ README.md
  └─ _system/vault.toml
```

It never overwrites a file, so it is safe to run again. Run it again after an upgrade
that adds a file to that list.

## `sessionmemory project`

Report which project a directory belongs to, or register it.

| Option       | What it does                                                    |
| ------------ | --------------------------------------------------------------- |
| `--register` | Create this directory's registry entry.                         |
| `--slug`     | Override the derived slug. Applies only with `--register`.       |
| `--cwd`      | Directory to resolve. Defaults to the shell's.                   |
| `--json`     | Emit JSON instead of prose.                                     |

```bash
sessionmemory project --register --cwd .
sessionmemory project
sessionmemory project --json
```

```
✓ registered 'invoice-api'
  └─ root: ~/repos/invoice-api
```

`--register` reads the git remote and the repository root, and derives the slug from the
remote name. It refuses a directory that is already registered, a bare repository, and a
slug another project holds. A slug is permanent once pages carry it, so moving a project
to a different slug is manual work.

With no `--register`, the command prints the entry as it stands and exits 1 when there is
none:

```
✓ invoice-api
  └─ root: ~/repos/invoice-api
```

`--json` prints every path a caller needs:

```json
{
  "slug": "invoice-api",
  "registered": true,
  "repo_root": "~/repos/invoice-api",
  "is_worktree": false,
  "project_dir": "~/repos/my-vault/projects/invoice-api",
  "paths": {
    "project_dir": "~/repos/my-vault/projects/invoice-api",
    "learnings": "~/repos/my-vault/projects/invoice-api/learnings",
    "logs": "~/repos/my-vault/projects/invoice-api/logs",
    "specs": "~/repos/my-vault/projects/invoice-api/specs",
    "plans": "~/repos/my-vault/projects/invoice-api/plans",
    "backlog": "~/repos/my-vault/projects/invoice-api/backlog.md"
  }
}
```

## `sessionmemory new learning`

Create a memory page in this project's learnings field.

| Option        | What it does                                     |
| ------------- | ------------------------------------------------ |
| `--title`     | The title. Required.                             |
| `--summary`   | One sentence a search result shows. Required.    |
| `--body`      | Markdown body.                                   |
| `--body-file` | Read the body from a file, or stdin for `-`.     |
| `--cwd`       | Directory to resolve the project from.           |
| `--json`      | Emit JSON instead of prose.                      |

```bash
sessionmemory new learning \
  --title "Stripe retries a webhook for 72 hours, so the handler must be idempotent" \
  --summary "Stripe redelivers an unacknowledged webhook for up to 72 hours, so the handler records the event id and ignores a repeat." \
  --cwd . --body-file - <<'EOF'
The handler inserts the Stripe event id into `webhook_events` before doing any work. A
duplicate key means the event was already handled, and the handler returns 200 without
touching the invoice. Source: https://docs.stripe.com/webhooks#retries
EOF
```

```
✓ created stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
  └─ ~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
```

The command writes the frontmatter and prints the path. Write the body into that file
afterwards, or pass it with `--body-file`. `--json` prints the path, the title, and the
uuid:

```json
{
  "path": "~/repos/my-vault/projects/invoice-api/learnings/invoice-numbers-come-from-a-postgres-sequence-never-from-max-id-plus-one.md",
  "title": "Invoice numbers come from a Postgres sequence, never from max(id) plus one",
  "uuid": "b58aec8c-bb77-478e-8bd2-5d7b148c398b"
}
```

The filename comes from the title. If that name is taken, the command takes the next free
one, so two pages with the same title never collide.

## `sessionmemory new spec`

Create a spec for this project. A spec is a plain file: it is never embedded and never
searched.

| Option        | What it does                                 |
| ------------- | -------------------------------------------- |
| `--title`     | The title. Required.                         |
| `--body`      | Markdown body.                               |
| `--body-file` | Read the body from a file, or stdin for `-`. |
| `--cwd`       | Directory to resolve the project from.       |
| `--json`      | Emit JSON instead of prose.                  |

```bash
sessionmemory new spec --title "Export invoices as UBL 2.1 XML" --cwd .
```

```
✓ created export-invoices-as-ubl-2-1-xml.md
  └─ ~/repos/my-vault/projects/invoice-api/specs/export-invoices-as-ubl-2-1-xml.md
```

Only `title`, `created`, and `updated` are written. After that the file is yours to edit.

## `sessionmemory new plan`

Create a plan for this project. It takes the same options as `sessionmemory new spec` and
writes into `plans/`.

```bash
sessionmemory new plan --title "Move PDF rendering to a worker queue" --cwd .
```

```
✓ created move-pdf-rendering-to-a-worker-queue.md
  └─ ~/repos/my-vault/projects/invoice-api/plans/move-pdf-rendering-to-a-worker-queue.md
```

## `sessionmemory log`

Record this session's work in the one page that belongs to it.

| Option         | What it does                                        |
| -------------- | --------------------------------------------------- |
| `--session-id` | This session's identifier. Required.                |
| `--title`      | The log's title. Required.                          |
| `--summary`    | One sentence a search result shows.                 |
| `--body`       | Markdown body. Replaces what is there.              |
| `--body-file`  | Read the body from a file, or stdin for `-`.        |
| `--cwd`        | Directory to resolve the project from.              |
| `--json`       | Emit JSON instead of prose.                         |

```bash
sessionmemory log --session-id 0f6c1f9a-4c1e-4a1b-9d2f-6f4b2a7c5e10 \
  --title "invoice-api 2026-09-02" \
  --summary "Made the Stripe webhook handler idempotent." \
  --body-file - --cwd . <<'EOF'
## Summary

Added the webhook_events table and the duplicate check in the handler.
EOF
```

```
✓ created 2026-09-02-invoice-api.md
  └─ ~/repos/my-vault/projects/invoice-api/logs/2026-09-02-invoice-api.md
```

The page is keyed on `--session-id`. A second call for the same session updates the page
it already wrote:

```
✓ updated 2026-09-02-invoice-api.md
  └─ ~/repos/my-vault/projects/invoice-api/logs/2026-09-02-invoice-api.md
```

CAUTION: The body replaces the page's whole body. Send the complete text every time,
never a diff and never an addendum.

## `sessionmemory search`

Find the pages nearest in meaning to the query, nearest first. This is the one read an
agent cannot do with its own tools.

| Option           | What it does                                                        |
| ---------------- | ------------------------------------------------------------------- |
| `{query}`        | What to look for, in plain words. Required.                         |
| `--logs`         | Search past session logs instead of learnings.                      |
| `--limit`        | Maximum number of results. Default 10.                              |
| `--max-distance` | Farthest cosine distance that still counts as a hit. Default 0.45.  |
| `--read`         | Print each hit's whole file under its path.                         |
| `--cwd`          | Directory to resolve the project from.                              |
| `--json`         | Emit JSON instead of prose.                                         |

```bash
sessionmemory search "why does the same stripe event arrive twice" --limit 3
```

```
~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
  Stripe retries a webhook for 72 hours, so the handler must be idempotent
  Stripe redelivers an unacknowledged webhook for up to 72 hours, so the handler records the event id and ignores a repeat.

~/repos/my-vault/projects/invoice-api/learnings/the-nightly-reconciliation-job-must-start-after-the-02-00-bank-feed.md
  The nightly reconciliation job must start after the 02:00 bank feed
  The bank feed lands at 02:00 UTC; a reconciliation run before it reports every open invoice as unpaid.

~/repos/my-vault/projects/invoice-api/learnings/invoice-numbers-come-from-a-postgres-sequence-never-from-max-id-plus-one.md
  Invoice numbers come from a Postgres sequence, never from max(id) plus one
  Two workers issuing invoices at once both read the same max(id), so numbering uses a database sequence.
```

Each result is the page's path, its title, and its summary. Read the path to get the
body, or pass `--read` to get every hit's whole file in the one call:

```bash
sessionmemory search "why does the same stripe event arrive twice" --limit 1 --read
```

```
~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
---
title: Stripe retries a webhook for 72 hours, so the handler must be idempotent
uuid: 1417edb7-4f54-45fc-95de-af21585cd271
summary: Stripe redelivers an unacknowledged webhook for up to 72 hours, so the handler records
  the event id and ignores a repeat.
created: '2026-09-02T17:34:56Z'
updated: '2026-09-02T17:34:56Z'
---
The handler inserts the Stripe event id into `webhook_events` before doing any work. A
duplicate key means the event was already handled, and the handler returns 200 without
touching the invoice. Source: https://docs.stripe.com/webhooks#retries
```

Each page is printed as it is on disk, under its path, with a blank line between pages.

A hit is a page within a cosine distance of the query, and a query nothing answers
returns nothing rather than the nearest pages dressed up as hits:

```bash
sessionmemory search "kubernetes ingress"
```

```
no results within distance 0.45; raise --max-distance to see farther pages
```

The default cutoff was measured on a real vault with this model: a page that answers the
query sits under 0.25, a related neighbor under 0.40, and the nearest page to an
unrelated query at 0.45 or beyond. `--max-distance` moves it, from 0 to 2.

Search refreshes the field's index before it queries, so a page written moments ago is
found without a `sessionmemory reindex` first. The first search on a machine downloads the
embedding model, about 520MB.

`--json` adds the cosine distance, where a smaller number is nearer, and with `--read`
adds each page's whole file as `content`:

```json
[
  {
    "path": "~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md",
    "title": "Stripe retries a webhook for 72 hours, so the handler must be idempotent",
    "summary": "Stripe redelivers an unacknowledged webhook for up to 72 hours, so the handler records the event id and ignores a repeat.",
    "distance": 0.28473490476608276
  },
  {
    "path": "~/repos/my-vault/projects/invoice-api/learnings/the-nightly-reconciliation-job-must-start-after-the-02-00-bank-feed.md",
    "title": "The nightly reconciliation job must start after the 02:00 bank feed",
    "summary": "The bank feed lands at 02:00 UTC; a reconciliation run before it reports every open invoice as unpaid.",
    "distance": 0.43256503343582153
  },
  {
    "path": "~/repos/my-vault/projects/invoice-api/learnings/invoice-numbers-come-from-a-postgres-sequence-never-from-max-id-plus-one.md",
    "title": "Invoice numbers come from a Postgres sequence, never from max(id) plus one",
    "summary": "Two workers issuing invoices at once both read the same max(id), so numbering uses a database sequence.",
    "distance": 0.4457415044307709
  }
]
```

`--logs` searches the project's session logs instead:

```bash
sessionmemory search "webhook handler work" --logs --limit 2
```

```
~/repos/my-vault/projects/invoice-api/logs/2026-09-02-invoice-api.md
  invoice-api 2026-09-02
  Made the Stripe webhook handler idempotent.
```

## `sessionmemory inject`

Print the block a session in this project starts with. The plugin runs this at session
start. Run it by hand to see what a session receives.

| Option      | What it does                                              |
| ----------- | --------------------------------------------------------- |
| `--cwd`     | Directory to resolve the project from.                    |
| `--command` | How the guidance names this CLI. Default `sessionmemory`. |
| `--json`    | Emit JSON instead of prose.                               |

```bash
sessionmemory inject --cwd ~/repos/invoice-api
```

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

No page body enters the block, and the index is never read. `--command` changes the
command name the guidance uses, which is how the plugin names an absolute path for a
session with no `sessionmemory` on its `PATH`.

`--json` emits the same content as six keys: `guidance`, `project`, `titles`,
`open_backlog`, `specs`, and `plans`.

## `sessionmemory reindex`

Bring this project's learnings and logs indexes up to date with the pages beside them.

| Option   | What it does                            |
| -------- | --------------------------------------- |
| `--cwd`  | Directory to resolve the project from.  |
| `--json` | Emit JSON instead of prose.             |

```bash
sessionmemory reindex --cwd .
```

```
✓ reindexing
  ├─ learnings: 4 added, 0 updated, 0 removed, 0 unchanged
  └─ logs: 0 added, 0 updated, 0 removed, 1 unchanged
```

`sessionmemory search` does the same refresh on every call, so this command is for doing
that work up front, or for rebuilding an index file you deleted.

```json
{
  "learnings": {
    "added": 0,
    "updated": 0,
    "removed": 0,
    "unchanged": 4
  },
  "logs": {
    "added": 0,
    "updated": 0,
    "removed": 0,
    "unchanged": 1
  }
}
```

## `sessionmemory delete`

Delete files from the vault permanently, dropping a page's index row with it.

| Option       | What it does                 |
| ------------ | ---------------------------- |
| `{targets}`  | Files to delete. Required.   |
| `--json`     | Emit JSON instead of prose.  |

```bash
sessionmemory delete "~/repos/my-vault/projects/invoice-api/learnings/Bad Name.md" \
  ~/repos/my-vault/projects/invoice-api/learnings/an-oversized-page.md
```

```
✓ deleted ~/repos/my-vault/projects/invoice-api/learnings/Bad Name.md
✓ deleted ~/repos/my-vault/projects/invoice-api/learnings/an-oversized-page.md
```

CAUTION: There is no confirmation and no dry run. The vault is a git repository, and its
history is what recovers a page you regret deleting.

A path outside the vault is refused and nothing is deleted. A path that does not exist
exits 1 after the other targets are removed.

## `sessionmemory doctor`

Report pages, projects, and indexes worth a look. Every finding is a suggestion, and the
command always exits 0.

| Option   | What it does                |
| -------- | --------------------------- |
| `--json` | Emit JSON instead of prose. |

```bash
sessionmemory doctor
```

```
1 suggestion
  └─ project: old-service: root ~/repos/old-service does not exist
```

```json
[
  {
    "check": "project",
    "path": "old-service",
    "message": "root ~/repos/old-service does not exist"
  }
]
```

See [Vault health](vault-health.md) for the six checks and what to do about each one.

## `sessionmemory export`

Write this project's field as a `.memoryfield.zip`, the format's archival transport.

| Option     | What it does                                                                  |
| ---------- | ----------------------------------------------------------------------------- |
| `--logs`   | Export the logs field instead of learnings.                                   |
| `--output` | Where to write the zip. Defaults to `<slug>.memoryfield.zip` in this directory, or `<slug>-logs.memoryfield.zip` with `--logs`. |
| `--cwd`    | Directory to resolve the project from.                                        |
| `--json`   | Emit JSON instead of prose.                                                   |

```bash
cd ~/repos
sessionmemory export --cwd ~/repos/invoice-api
```

```
✓ exported 4 pages
  └─ ~/repos/invoice-api.memoryfield.zip
```

The archive holds every page and a fresh index, flat at the zip root:

```
Archive:  ~/repos/invoice-api.memoryfield.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
      313  09-02-2026 13:34   invoice-numbers-come-from-a-postgres-sequence-never-from-max-id-plus-one.md
      364  09-02-2026 13:34   pytest-asyncio-needs-asyncio-mode-auto-or-every-async-test-is-skipped.md
      573  09-02-2026 13:34   stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
      305  09-02-2026 13:34   the-nightly-reconciliation-job-must-start-after-the-02-00-bank-feed.md
    28672  09-02-2026 13:35   nomic-embed-text-v1.5.sqlite3
---------                     -------
    30227                     5 files
```

## Environment variables

| Variable                    | What it does                                                          |
| --------------------------- | --------------------------------------------------------------------- |
| `SESSIONMEMORY_VAULT`       | Where the vault is. Every command needs it.                           |
| `SESSIONMEMORY_MODEL_CACHE` | Where the embedding model is cached. Defaults to `~/.cache/sessionmemory/models`. |
| `SESSIONMEMORY_EMBEDDER`    | Test only. `stub` selects a hash-derived embedder and loads no model. |

## What is not built

- **No cross-project search.** A page belongs to one project and is reachable from that
  project. There is no command that searches the whole vault.
- **No global pages.** Knowledge that applies everywhere belongs in a skill or in a
  repository's own `CLAUDE.md`.
- **No ranking or limit on injection.** A session receives every title. When a project's
  list grows too long to read, the answer is deleting pages.
- **No `--fix`.** `sessionmemory doctor` reports and never repairs.
