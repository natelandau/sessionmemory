# Managing a sessionmemory vault

The sessionmemory vault holds a coding agent's durable memory as markdown. This guide
covers managing the vault as a whole for a person: its projects, its health, its indexes,
and its private files. Writing memory during a coding session and the plugin's
`/sessionmemory:*` slash commands are out of scope; they have their own instructions.

## Rules

- Work on the whole vault unless the person names one project. If the current directory
  belongs to a registered project (`sessionmemory project --json` exits 0), ask whether
  to focus on that project or on the whole vault before you report anything.
- Never run `sessionmemory init`; it is the person's bootstrap command. If no vault is
  configured, say so and stop.
- Ask before anything that removes, moves, or overwrites content: deleting a file,
  editing or removing a registry entry, renaming or splitting a page, rewriting logs, and
  any `sessionmemory local` command other than `status`. Say what will change, then wait
  for a yes. This covers every "Ask first" below.
- Before any delete, run `git status` in the vault. If earlier work is uncommitted, tell
  the person and wait for them to commit it, since git history is the only way to
  recover a deleted file.
- The CLI creates, searches, and reports. It never repairs. Make each fix with your own
  file tools, then run `sessionmemory doctor --json` again to confirm it.
- Add `--json` to every command whose output you parse. JSON output is never styled.
- Never write a credential into the vault; it is a committed git repository. Record where
  the credential lives instead.
- Never commit the vault. With the sessionmemory plugin installed, its hooks commit at
  session start and end. Without it, committing is the person's job.

## Find the vault

The CLI reads `SESSIONMEMORY_VAULT`, then `[vault] root` in
`~/.claude/sessionmemory.toml`. The variable wins when both are set. Inside a registered
checkout, `project_dir` in `sessionmemory project --json` output is
`<vault>/projects/<slug>`. Never take the vault's location from `doctor` output: a clean
vault reports no findings, and so no paths.

## Vault layout

```
<vault>/
  _system/vault.toml                 marks the directory as a vault
  _system/registry.toml              registered projects: slug, git remotes, checkout root
  projects/<slug>/
    learnings/<name>.md              a field: one fact per page, searched by meaning
    logs/<date>-<time>.md            a field: one page per past session
    specs/<date>-<name>.md           one design record per feature, indexed for search
    plans/<date>-<name>.md           scratch, never committed
    runbooks/<name>.md               step-by-step procedures to follow by hand
    reference/<name>.md              private docs about the project
    backlog.md                       open work, one top-level bullet per item
    local/                           private files linked into each checkout
    */<model>.sqlite3                search indexes: derived, gitignored, disposable
```

A project's files are found by its slug only. Nothing is shared between projects, and no
command searches across projects.

## Projects and the registry

`_system/registry.toml` holds one table per project:

```toml
[projects.invoice-api]
remotes = ["github.com/acme/invoice-api"]
root = "/Users/me/repos/invoice-api"
reviewed = "2026-10-01"
```

- `sessionmemory project --json --cwd <dir>` shows which project owns a directory, and
  every vault path for it.
- `sessionmemory project register --cwd <dir>` registers a directory. The plugin does
  this for a git repository at session start, so run it by hand only for a folder outside
  git or to choose a slug with `--slug`.
- `sessionmemory project mark-reviewed --cwd <dir>` records today as the date the
  project's learnings were last reviewed.
- To list every project, read `registry.toml`, and list `projects/` too: a folder there
  with no registry entry is left behind by a project that was unregistered.
- A project whose checkout moved: edit its `root`. A project that is gone for good:
  remove its table, and delete `projects/<slug>/` only if the person also wants its pages
  gone. A slug is permanent once pages carry it. Renaming one is manual work.

## Pages

A learnings page has frontmatter with `title`, `uuid`, `summary`, `created`, and
`updated`, then a prose body. The title is what a session sees at its start, and the
summary is what a search shows, so a page missing either one cannot be found. Logs pages
can have no frontmatter. Rules for a page in `learnings/` or `logs/`:

- A filename is lowercase ASCII letters, digits, and hyphens, starting and ending with a
  letter or digit. Any other name is not a page: it is never indexed or searched.
- A page is at most 8192 bytes. Only the first 8192 bytes are embedded, so a longer
  page's tail is invisible to search. Split it into two pages, each with its own title and
  summary.
- Dates in frontmatter are quoted: `created: '2026-09-02T10:00:00Z'`.
- A page that is wrong or out of date is deleted, not marked. Pages have no status.

A spec is the one file with a status: `draft`, `shipped`, `superseded`, or `abandoned`,
or no status, which means current. A superseded spec names its replacement with
`superseded_by: <filename>`.

`backlog.md` has no checkboxes. Every top-level bullet is an open item, and indented
bullets under it are its detail. Finished work is deleted with its detail, never ticked.

## Commands for managing the vault

| Command                                         | Use it to                                             |
| ----------------------------------------------- | ----------------------------------------------------- |
| `sessionmemory doctor --json`                   | Report everything off in the vault. Never edits.      |
| `sessionmemory reindex --all --json`            | Bring every project's indexes up to date.             |
| `sessionmemory reindex --cwd <dir> --json`      | Bring one project's indexes up to date.               |
| `sessionmemory reconcile --dry-run --cwd <dir>` | Preview fixing commit lists in one project's logs.    |
| `sessionmemory reconcile --cwd <dir>`           | Rewrite those commit lists. Ask first.                |
| `sessionmemory delete <file>...`                | Delete vault files and their index rows. Ask first.   |
| `sessionmemory search "<words>" --cwd <dir>`    | Find learnings by meaning. Add `--logs` or `--specs`. |
| `sessionmemory path --json --cwd <dir>`         | Print every vault path for one project.               |
| `sessionmemory export --cwd <dir>`              | Write a project's learnings as a `.memoryfield.zip`.  |
| `sessionmemory local status --json --cwd <dir>` | Compare a checkout's private files with the vault.    |

Commands other than `doctor`, `delete`, and `reindex --all` act on one project, chosen by
`--cwd`, a directory inside that project's checkout. Run `sessionmemory <command> --help`
for every option.

`delete` has no confirmation prompt and no dry run, so the person's yes is the only check.

`reconcile` leaves a line it cannot prove right as it is and counts it as unchecked. Each
warning gives the reason and lists the logs that hold the line. With `--json`,
`note_logs` maps each reason in `notes` to those logs. Show the warnings to the person.
Edit a line only when its warning names the fix, and ask first.

## Doctor findings and their fixes

`sessionmemory doctor --json` prints a list of findings, each with `check`, `path`,
`message`, and `project`. `project` is empty for a finding about the vault itself. Every
finding is a suggestion, and the command always exits 0.

| Check         | What is wrong                                           | Fix                                                                        |
| ------------- | ------------------------------------------------------- | -------------------------------------------------------------------------- |
| `filename`    | A page's name breaks the filename rule                  | Rename the file to a conforming name.                                      |
| `size`        | A page is over 8192 bytes                               | Split it into two pages, each with a title and summary.                    |
| `frontmatter` | Frontmatter cannot be parsed, or lacks title or summary | Correct the block. Write a missing title or summary from the body.         |
| `datetime`    | A frontmatter date is unquoted                          | Quote the value.                                                           |
| `project`     | A registered root no longer exists                      | Ask whether it moved (edit `root`) or is gone (remove the entry).          |
| `registry`    | `registry.toml` cannot be read                          | Fix the TOML by hand. Every other command fails until then.                |
| `backlog`     | A backlog line is ticked `- [x]`                        | Delete the line and its detail.                                            |
| `reference`   | A file names a vault file that does not exist           | Correct the name, write the missing file, or remove the mention.           |
| `spec`        | A spec's `status` or `superseded_by` is not usable      | Set a valid status, or name the real replacement spec.                     |
| `commits`     | A log lists commits no branch holds                     | Run `reconcile --dry-run`, show the person, then `reconcile`.              |
| `index`       | An index is behind its files or cannot be opened        | Run `sessionmemory reindex --all`.                                         |
| `local`       | A private file or its link has a problem                | Follow the remedy in the message. Ask before moving or overwriting a file. |
| `doctor`      | A check itself failed                                   | Report the message. The other checks still ran.                            |

When a finding's message names a remedy, follow the message over the table.

Report findings grouped by project, in this order: `filename`, `frontmatter`, and `size`
first, since they hide memory from search, then `registry` and `project`, then the rest.
Fix them in the order the person picks.

## Private local files

Each project's `local/` folder holds files that must never reach the project's
repository: `CLAUDE.local.md`, private skills under `.claude/skills/`, private rules under
`.claude/rules/`, other paths adopted by name, and `.claude/settings.local.json`. Each
checkout holds an absolute symlink to the vault copy, ignored through git's
`info/exclude`. The settings file is copied, not linked, and the person picks which copy
wins.

- `sessionmemory local status --json --cwd <dir>` reports each entry's state. It changes
  nothing.
- `sessionmemory local sync --cwd <dir>` creates missing links and replaces broken ones.
  Without a terminal it asks nothing and reports what is still pending.
- `sessionmemory local adopt <entry> --cwd <dir>` moves a checkout's file into the vault
  and links it back.
- `sessionmemory local decline <entry> --cwd <dir>` records that an entry stays out of
  the vault, so nothing offers it again.
- `sessionmemory local sync --keep-settings checkout|vault --cwd <dir>` writes one
  settings copy over the other. Show the person the difference that `local sync` reports
  and let them choose.

## The index

Each field, and `specs/`, has one SQLite index file named for the embedding model. It is
derived from the markdown and safe to delete. `sessionmemory search` refreshes the index
it reads, so a stale index is never wrong for long. After cloning a vault, or when
`doctor` reports an index problem, run `sessionmemory reindex --all`.

## Settings

Settings live in `~/.claude/sessionmemory.toml`, and a project can override any key in
`<checkout>/.claude/sessionmemory.toml`. Every key is optional.

| Key              | Default | What it does                                             |
| ---------------- | ------- | -------------------------------------------------------- |
| `vault.root`     | unset   | Where the vault is                                       |
| `inject.enabled` | `true`  | Show the vault's titles at session start                 |
| `sweep.enabled`  | `true`  | Record each session into the vault when it ends          |
| `local.link`     | `true`  | Link private files into the checkout at session start    |
| `log.level`      | `info`  | The plugin's log level: debug, info, warning, error, off |

The plugin's hooks write their own log. To find it, run `hooks/vault-path.py --log` from
the plugin's install folder under `~/.claude/plugins/cache/`.

## Suggestions to offer

When the person asks what is worth doing, look for:

- Doctor findings, grouped and ordered as in the doctor section.
- Registered projects whose checkout is gone, and project folders with no registry entry.
- A project with 10 or more learnings whose `reviewed` date is missing or more than 30
  days old. Suggest the person run `/sessionmemory:review` in that project.
- A long `backlog.md`. Suggest the person run `/sessionmemory:backlog` in that project.
- Index problems across projects, fixed by one `sessionmemory reindex --all`.
