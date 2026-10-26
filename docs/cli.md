# CLI reference

Every command below runs as `sessionmemory <command>`. The `sessionmemory` command comes
from `uv tool install sessionmemory`, as [the README](../README.md#install) describes.

## Conventions

Every command reads `SESSIONMEMORY_VAULT` to find the vault. When the variable is unset,
the command reads `vault.root` from `~/.claude/sessionmemory.toml` instead, the same file
the plugin reads, and the variable wins when both are set. With neither set, or with the
vault pointing at a directory that `sessionmemory init` never touched, the command exits 1
and says so:

```
✗ SESSIONMEMORY_VAULT is not set. Point it at your vault repository.
  ├─ export SESSIONMEMORY_VAULT=/path/to/your/vault
  └─ or set [vault] root in ~/.claude/sessionmemory.toml
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

| Flag              | What it does                                                           |
| ----------------- | ---------------------------------------------------------------------- |
| `--json`          | Print a machine-readable payload instead of prose. Never styled.       |
| `--cwd <path>`    | Resolve the project from that directory instead of the shell's.        |
| `-v`, `-vv`       | Increase output verbosity. Repeat for more.                            |
| `--version`       | Print the installed version bare and exit. The plugin's hooks read it. |
| `--body-file <p>` | Read a markdown body from a file, or from stdin for `-`.               |

A command the installed version does not have names that version and the upgrade command,
since a plugin newer than the tool on `PATH` is the usual cause:

```
Usage: sessionmemory [OPTIONS] COMMAND [ARGS]...
Try 'sessionmemory --help' for help.
╭─ Error ──────────────────────────────────────────────────────────────────────╮
│ No such command 'frobnicate'.                                                │
│ Installed sessionmemory is 0.9.0; a newer release may have it.               │
│ Upgrade with: uv tool upgrade sessionmemory                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

A name close to a real command is a typo rather than a missing feature, so the error
suggests the command and names the version without pointing at a newer release:

```
Usage: sessionmemory [OPTIONS] COMMAND [ARGS]...
Try 'sessionmemory --help' for help.
╭─ Error ──────────────────────────────────────────────────────────────────────╮
│ No such command 'projects'. Did you mean 'project'?                          │
│ Installed sessionmemory is 0.9.0.                                            │
│ Upgrade with: uv tool upgrade sessionmemory                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

The output below comes from real runs against a vault at `~/repos/my-vault` holding one
registered project, `invoice-api`, checked out at `~/repos/invoice-api`. The CLI prints
absolute paths; the home directory is shortened to `~` here.

## `sessionmemory init`

Create the files a new vault needs.

| Option        | What it does                                                              |
| ------------- | ------------------------------------------------------------------------- |
| `[directory]` | Where to create the vault. Defaults to the configured vault root.         |
| `--force`     | Initialize a directory that already has contents. Nothing is overwritten. |
| `--json`      | Emit JSON instead of prose.                                               |

```bash
sessionmemory init                       # in the configured vault root
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
that adds a file to that list. The `.gitignore` it writes ignores the derived index files
and `projects/*/plans/`, since a plan is scratch that never belongs in history. It also
re-includes each project's `local/` folder and everything in it, so a global ignore rule
cannot keep a private file, skill, or rule out of the vault. Inside `local/` it still
ignores `.DS_Store`, `__pycache__/`, `node_modules/`, `.venv/`, and `venv/`. The
`local` commands add these lines to an older vault's `.gitignore` before they move a
file into `local/`.

## `sessionmemory project`

Report which project a directory belongs to, or register it.

| Option            | What it does                                                    |
| ----------------- | --------------------------------------------------------------- |
| `--register`      | Create this directory's registry entry.                         |
| `--slug`          | Override the derived slug. Applies only with `--register`.      |
| `--mark-reviewed` | Record today as the day this project's learnings were reviewed. |
| `--cwd`           | Directory to resolve. Defaults to the shell's.                  |
| `--json`          | Emit JSON instead of prose.                                     |

```bash
sessionmemory project --register --cwd .
sessionmemory project
sessionmemory project --json
```

```
✓ registered 'invoice-api'
  └─ root: ~/repos/invoice-api
```

`--register` reads the git remote and the repository root, derives the slug from the
remote name, and creates the project's folder with an empty `learnings/` and `logs/`
inside it. It refuses a directory that is already registered, a bare repository, and a
slug another project holds. A slug is permanent once pages carry it, so moving a project
to a different slug is manual work. The plugin runs this command for a git repository at
session start. By hand, it registers a directory outside git or applies a chosen `--slug`.

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
    "runbooks": "~/repos/my-vault/projects/invoice-api/runbooks",
    "reference": "~/repos/my-vault/projects/invoice-api/reference",
    "backlog": "~/repos/my-vault/projects/invoice-api/backlog.md",
    "local": "~/repos/my-vault/projects/invoice-api/local"
  }
}
```

`--mark-reviewed` stores `reviewed = "YYYY-MM-DD"` on the project's entry in
`_system/registry.toml`. `/sessionmemory:review` runs it when it finishes, and the session
start stops nudging for a review until the date is more than 30 days old:

```
✓ reviewed 'invoice-api'
  └─ root: ~/repos/invoice-api
```

A date edited in by hand may be written bare, as `reviewed = 2026-08-01`, and reads the
same. Any other value reads as never reviewed rather than as a broken registry.

## `sessionmemory path`

Print one absolute path for this project and nothing else, so a script can use it
directly.

| Option   | What it does                                                                                                |
| -------- | ----------------------------------------------------------------------------------------------------------- |
| `<kind>` | `project`, `learnings`, `logs`, `specs`, `plans`, `runbooks`, `reference`, `backlog`, or `local`. Required. |
| `--cwd`  | Directory to resolve the project from.                                                                      |

```bash
sessionmemory path specs --cwd .
```

```
~/repos/my-vault/projects/invoice-api/specs
```

```bash
sessionmemory path backlog --cwd .
```

```
~/repos/my-vault/projects/invoice-api/backlog.md
```

```bash
sessionmemory path local --cwd .
```

```
~/repos/my-vault/projects/invoice-api/local
```

`local` is the folder that holds the project's entries for
[`sessionmemory local`](#sessionmemory-local). The path is printed whether or not the file
or folder exists yet. `sessionmemory project --json` prints every path at once.

## `sessionmemory local`

Keep this project's private Claude Code files in the vault and sync them with each
checkout. These files hold personal instructions and settings that must not reach the
project's repository. Without this command each one exists in one clone only. In the vault
it follows you to your other machines and to every git worktree of the project.

The command manages five kinds of entry. The name in the first column is what you type
and what every output shows:

| Entry                 | In the checkout               | In the vault, under `projects/<slug>/` |
| --------------------- | ----------------------------- | -------------------------------------- |
| `CLAUDE.local.md`     | `CLAUDE.local.md`             | `local/CLAUDE.local.md`                |
| `skills/<name>`       | `.claude/skills/<name>/`      | `local/claude/skills/<name>/`          |
| `rules/<name>`        | `.claude/rules/<name>`        | `local/claude/rules/<name>`            |
| A path you adopt      | `.claude/agents`              | `local/claude/agents`                  |
| `settings.local.json` | `.claude/settings.local.json` | `local/claude/settings.local.json`     |

The command finds `CLAUDE.local.md`, skills, and rules by their location. Any other file
or folder becomes an entry only when you adopt it by its path, as
[Adopting any path](#adopting-any-path) describes. Its name is its path from the top of
the work tree. In the vault it keeps the same path, with `.claude/` stored as `claude/`.

A skill is a folder. A rule is the file or the folder directly inside `.claude/rules/`,
and a rules folder is linked as one entry. A name that starts with a dot is never an
entry, and neither is a plain file in `.claude/skills/`. Each entry has its own link, so
a skill or rule that the project commits can sit in the same folder as your private ones.

Each link is an absolute symlink in the git work tree that holds `--cwd`, which is not
always the registered root. Claude Code reads a link as it reads a regular file, and an
edit made through Claude Code changes the vault copy. Git ignores each link through the
repository's `info/exclude`, never through a committed `.gitignore`. A skill or rule gets
an anchored line, such as `/.claude/skills/deploy-check`, so a file with the same name
deeper in the tree stays visible to git.

`settings.local.json` is copied, not linked. Claude Code refuses to write through a
symlinked settings file. With a link in its place, a permission you approve, a plugin
installed at local scope, and every other local-scope change fails or is lost. Claude
Code itself writes the file during sessions, so either copy can be the newer one. The
command never copies it in either direction until you choose which copy to keep.

Each sub-command resolves the project from `--cwd`, and refuses an unregistered directory
and a directory outside git. An entry that git tracks belongs to the project. The
sub-commands never offer it, adopt it, copy it, or link over it.

| Sub-command | What it does                                                                  |
| ----------- | ----------------------------------------------------------------------------- |
| `sync`      | Link the vault's entries into this checkout, then adopt and copy as you chose |
| `status`    | Report how each entry in this checkout relates to the vault                   |

Both sub-commands take `--cwd`, the directory to resolve the project from, and `--json`.

`sync` and `status` first look for a `CLAUDE.local.md` directly in the project's vault
folder, outside `local/`. They move it into `local/`. If `local/` already holds an
identical copy, they delete the outer one. If the two copies differ, nothing moves, and
the command names both paths:

```
! ~/repos/my-vault/projects/invoice-api/CLAUDE.local.md and ~/repos/my-vault/projects/invoice-api/local/CLAUDE.local.md differ, so CLAUDE.local.md was not moved into local/
  └─ Keep one of them, delete the other, then run local sync.
```

### `sessionmemory local status`

Report the state of every entry, from the checkout and from the vault.

```bash
sessionmemory local status --cwd .
```

```
unadopted      CLAUDE.local.md
unadopted      skills/deploy-check
unadopted      rules/style.md
checkout only  settings.local.json
local: ~/repos/my-vault/projects/invoice-api/local
```

Each line gives the state and the entry name. The last line is the project's `local/`
folder in the vault. A worktree whose settings differ from the vault copy reports this:

```
linked         CLAUDE.local.md
linked         skills/deploy-check
linked         rules/style.md
differs        settings.local.json
local: ~/repos/my-vault/projects/invoice-api/local
```

A linked entry is in one of six states:

| State          | Meaning                                                               |
| -------------- | --------------------------------------------------------------------- |
| `linked`       | The checkout holds a link to the vault copy                           |
| `not linked`   | The vault holds a copy, and the checkout has nothing there            |
| `unadopted`    | The checkout holds a real file or folder, and the vault holds no copy |
| `diverged`     | The checkout holds a real file or folder, and the vault holds a copy  |
| `foreign link` | The checkout holds a link that resolves somewhere else                |
| `dangling`     | The checkout holds a link to nothing                                  |

`settings.local.json` is in one of seven states. It is left out when neither side has a
copy:

| State           | Meaning                                                                 |
| --------------- | ----------------------------------------------------------------------- |
| `same`          | Both copies hold the same settings                                      |
| `differs`       | Both copies exist and hold different settings                           |
| `checkout only` | The checkout has a copy, and the vault has none                         |
| `vault only`    | The vault has a copy, and the checkout has none                         |
| `linked`        | The checkout holds a symlink, which Claude Code cannot write through    |
| `tracked`       | Git tracks the checkout's copy                                          |
| `blocked`       | `.claude/` is a symlink or not a folder, so nothing is read through it |

Two settings copies are the same when they hold the same JSON, so a change of indent or
key order is not a difference. A copy that is not valid JSON is compared byte for byte.

A note in parentheses follows a linked entry's name only when something needs attention.
`tracked` means that git tracks the entry. `not ignored` means that git does not ignore a
link. `contents identical` or `contents different` follows a `diverged` entry. An editor
that saves by writing a temporary file and renaming it over the link also leaves a
`diverged` state behind. A skill that is a symlink into another repository shows as
`foreign link` and stays as it is.

`--json` prints the `local` folder and one object per entry:

```json
{
  "local": "~/repos/my-vault/projects/invoice-api/local",
  "entries": [
    {
      "kind": "local-md",
      "name": "CLAUDE.local.md",
      "state": "linked",
      "link": "~/repos/invoice-api/CLAUDE.local.md",
      "target": "~/repos/my-vault/projects/invoice-api/local/CLAUDE.local.md",
      "ignored": true,
      "tracked": false,
      "match": null
    },
    {
      "kind": "skill",
      "name": "skills/deploy-check",
      "state": "linked",
      "link": "~/repos/invoice-api/.claude/skills/deploy-check",
      "target": "~/repos/my-vault/projects/invoice-api/local/claude/skills/deploy-check",
      "ignored": true,
      "tracked": false,
      "match": null
    },
    {
      "kind": "rule",
      "name": "rules/style.md",
      "state": "linked",
      "link": "~/repos/invoice-api/.claude/rules/style.md",
      "target": "~/repos/my-vault/projects/invoice-api/local/claude/rules/style.md",
      "ignored": true,
      "tracked": false,
      "match": null
    }
  ]
}
```

`kind` is `local-md`, `skill`, `rule`, `path`, or `settings`. A `path` row is a file or
folder you adopted by its path, and its `name` is that path, such as `.claude/agents`. The settings row has its copy state
in `state` and `match` set to `null`. `ignored` is `null` when `.claude/` or its
`skills/` or `rules/` folder is a symlink or not a folder, since git cannot answer for a
path behind it. `match` is `true` or `false` when both sides are real files or both are
real folders, and `null` otherwise. Two folders match when they hold the same files with
the same bytes.

### `sessionmemory local sync`

Link every entry that the vault holds into this checkout, then adopt entries into the
vault and keep a settings copy as you choose.

```bash
sessionmemory local sync --cwd .
```

| Option                            | What it does                                                                                         |
| --------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `--adopt NAME`                    | Move the entry, or any file or folder named by its path, into the vault and link it back. Repeatable |
| `--keep-settings checkout\|vault` | Write that copy of `settings.local.json` over the other one                                          |
| `--json`                          | Emit JSON instead of prose                                                                           |
| `--cwd DIR`                       | Directory to resolve the project from                                                                |

Every run does the linking that can never lose content. It creates a missing link,
replaces a link to nothing, and deletes a link whose vault entry is gone. Nothing that
moves or overwrites your files happens without your choice: adopting an entry needs
`--adopt`, and copying settings needs `--keep-settings`.

On a terminal with no options, the command links, then asks one question per pending
item. Each answer says what it does, and a no or a cancel leaves the file alone:

- An entry only in the checkout: `Track .claude/skills/deploy-check in the vault?` Yes
  moves it into the vault and links it back.
- Settings only in the checkout: `Track .claude/settings.local.json in the vault?` Yes
  copies it into the vault, and the checkout keeps its copy.
- Settings only in the vault: `The vault has a .claude/settings.local.json for this
  project, but this checkout has none. Copy it into this checkout?`
- Settings that differ: `.claude/settings.local.json differs between this checkout and
  the vault. Which copy wins?`

For differing settings, the answers are this checkout's copy, the vault's copy, or
neither. Each copy's answer shows its modification time, and a summary of what each copy
has that the other lacks prints above the question.

Off a terminal with no options, the command never prompts. It links, then names each
pending item and the command that resolves it, and exits 0:

```
! not tracked in the vault yet: CLAUDE.local.md, .claude/skills/deploy-check, .claude/rules/style.md
  └─ Track them in the vault with: sessionmemory local sync --adopt CLAUDE.local.md --adopt skills/deploy-check --adopt rules/style.md --cwd .
! .claude/settings.local.json is not tracked in the vault yet
  └─ Track it in the vault with: sessionmemory local sync --keep-settings checkout --cwd .
```

When the settings differ, the warning lists what each copy has that the other lacks:

```
! .claude/settings.local.json differs between this checkout and the vault
  ├─ permissions.allow: 1 only in the checkout (Bash(just docs))
  ├─ enabledPlugins: only in the checkout
  ├─ Copy this checkout's over the vault's with: sessionmemory local sync --keep-settings checkout --cwd .
  └─ Copy the vault's over this checkout's with: sessionmemory local sync --keep-settings vault --cwd .
```

Lists such as `permissions.allow` compare item by item, in any order. The modification
time of each copy is in the JSON output and in the question. It is a hint, not a rule:
a vault copy that arrived through `git pull` carries the time of the pull.

The command prints each path it wrote on stdout and nothing else. Adopting all three
entries and keeping the checkout's settings prints this:

```bash
sessionmemory local sync --adopt CLAUDE.local.md --adopt skills/deploy-check --adopt rules/style.md --keep-settings checkout --cwd .
```

```
! Claude Code asks once per checkout to allow a linked rule as an external import
! the hooks commit the vault copy to the vault's git remote
~/repos/my-vault/projects/invoice-api/local/CLAUDE.local.md
~/repos/my-vault/projects/invoice-api/local/claude/skills/deploy-check
~/repos/my-vault/projects/invoice-api/local/claude/rules/style.md
~/repos/my-vault/projects/invoice-api/local/claude/settings.local.json
```

In a second worktree, the same command links the entries and asks about the settings:

```
~/repos/invoice-api-pdf/CLAUDE.local.md
~/repos/invoice-api-pdf/.claude/skills/deploy-check
~/repos/invoice-api-pdf/.claude/rules/style.md
! the vault has a .claude/settings.local.json that this checkout lacks
  └─ Copy it into this checkout with: sessionmemory local sync --keep-settings vault --cwd .
```

`--keep-settings vault` writes the vault copy into `.claude/settings.local.json`, creates
`.claude/` when it is missing, and adds the file to `info/exclude`. Claude Code read its
settings before the command ran. Start a new session before you approve anything, or
Claude Code can write its old copy back:

```
! Claude Code read its settings before this ran; start a new session before approving anything so it does not write the old copy back
~/repos/invoice-api-pdf/.claude/settings.local.json
```

`--keep-settings` refuses a settings file that git tracks, one behind a symlinked
`.claude/`, a checkout copy that is a symlink, and a side that has no copy. When both
copies are the same, it does nothing.

With `--json`, the command prints one object per vault entry it linked, and the pending
work:

```json
{
  "entries": [
    {
      "kind": "local-md",
      "name": "CLAUDE.local.md",
      "status": "present",
      "link": "~/repos/invoice-api-pdf/CLAUDE.local.md",
      "target": "~/repos/my-vault/projects/invoice-api/local/CLAUDE.local.md",
      "reason": null,
      "remedy": null
    }
  ],
  "pending": {
    "adopt": [],
    "settings": {
      "state": "vault only",
      "checkout": {
        "path": "~/repos/invoice-api-pdf/.claude/settings.local.json",
        "mtime": null
      },
      "vault": {
        "path": "~/repos/my-vault/projects/invoice-api/local/claude/settings.local.json",
        "mtime": "2026-10-26T17:09:23Z"
      },
      "summary": []
    }
  },
  "manifest_problems": []
}
```

`pending.adopt` names each entry that the command can adopt: an untracked entry that is
`unadopted`, or `diverged` with identical contents. `pending.settings` is `null` unless
the settings state is `differs`, `checkout only`, or `vault only`. `reason` and `remedy`
are set for a `conflict`. An `adopted` list follows when `--adopt` ran, with the `name`
and vault `target` of each entry adopted. A `settings` object follows when
`--keep-settings` ran, with `kept` and the `path` that holds the kept copy. When both
copies were already the same, nothing is written and `path` names the existing copy.
`manifest_problems` holds one line for each line of `local/entries.toml` that the command
cannot use, and is empty when there is none.

When nothing is linked or pending, the command says so on stderr and exits 0:

```
nothing to sync for this project
```

Each vault entry gets one link status:

| Status     | Meaning                                                                                                          |
| ---------- | ---------------------------------------------------------------------------------------------------------------- |
| `created`  | The checkout had nothing there, or a link to nothing. The command made the link                                  |
| `present`  | The checkout already holds a link to the vault copy                                                              |
| `tracked`  | Git tracks the checkout path. Nothing changed, and the vault copy stays in the vault                             |
| `conflict` | Something is in the way, or the vault entry has the wrong shape. Nothing changed                                 |
| `removed`  | A link in `.claude/skills/` or `.claude/rules/` pointed at a `local/` entry that is gone. The command deleted it |

`sync` never replaces a file or folder that holds content, and it never deletes a link to
nothing that points outside `local/`. Each conflict gets a warning on stderr that names
the cause and the remedy for that cause. Each tracked entry also gets a warning:

```
! ~/repos/my-vault/projects/invoice-api/CLAUDE.local.md and ~/repos/my-vault/projects/invoice-api/local/CLAUDE.local.md differ, so CLAUDE.local.md was not moved into local/
  └─ Keep one of them, delete the other, then run local sync.
! the vault copy at ~/repos/my-vault/projects/invoice-api/local/claude/skills/notes.md is not a folder
  └─ Fix or remove it in the vault, then run local sync.
! ~/repos/invoice-api/.claude/skills/release-notes is tracked by git; the vault copy stays in the vault
! ~/repos/invoice-api/.claude/rules/style.md differs from the vault copy at ~/repos/my-vault/projects/invoice-api/local/claude/rules/style.md
  └─ Copy the newer one over the vault copy, delete the checkout copy, and run local sync.
```

These are the causes of a conflict, and the remedy that the warning gives for each:

| Cause                                                                       | Remedy                                                                             |
| --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| The old-path copy and the `local/` copy differ                              | Keep one of them, delete the other, run `local sync`                               |
| The vault entry is not of its kind's shape, such as a file among the skills | Fix or remove it in the vault, run `local sync`                                    |
| A checkout copy differs from the vault copy                                 | Copy the newer one over the vault copy, delete the checkout copy, run `local sync` |
| A checkout copy is identical to the vault copy                              | Run `local sync --adopt NAME`, which replaces it with the link                     |
| The checkout holds a link to somewhere else                                 | Remove it and run `local sync`, or compare with `local status`                     |
| `.claude/` or its `skills/` or `rules/` folder is a symlink or a file       | Replace it with a real folder, run `local sync`                                    |

A removed link is reported the same way:

```
! removed ~/repos/invoice-api/.claude/rules/review.md, whose vault entry is gone
```

The command exits 1 when any linked entry is a `conflict`, when an `--adopt` or a
`--keep-settings` failed, or when `local/entries.toml` holds a line that the command
cannot use. `--json` still prints the whole payload. One conflict never
stops the other entries. A conflict for an entry that the same run then adopts is not
reported.

To resolve a `diverged` entry, copy the newer content over the vault copy, delete the
checkout copy, and run `local sync`. Deleting the vault copy instead leaves `sync` with
nothing to link. The plugin runs `local sync --json` at every session start (see
[SessionStart](plugin.md#sessionstart)). On `created` and on `present`, `sync` also adds
the `info/exclude` line when git does not ignore the link, so a fresh clone is covered
and a lost line comes back.

#### Adopting an entry

`--adopt` moves an entry from this checkout into the vault and replaces it with a link.
The command does these steps for each entry, in order:

1. If the entry is already a link to the vault copy, it adds the `info/exclude` line when
   git does not ignore the link, prints the vault path, and goes to the next entry.
2. It refuses the entry when `.claude/` or its `skills/` or `rules/` folder is a symlink,
   when git tracks the entry, when a link points somewhere other than the vault copy, and
   when the checkout holds nothing. It refuses a skill that is not a folder, and a folder
   that holds a symlink, which it names. It also refuses a folder that holds a
   `node_modules`, `.venv`, `venv`, or `.git` folder at any depth, and names that folder.
3. If the vault copy exists and differs from the entry, it refuses and names both paths.
   You choose which version wins.
4. It refuses a `CLAUDE.local.md` or a rule `.md` file that is not valid UTF-8. If the
   vault holds no copy, it writes the copy and compares it with the original. A folder is
   compared file by file, and the copy keeps the executable bit. On a mismatch it removes
   the copy and leaves the original untouched. If a vault copy appears while the command
   runs, such as from a second `sync --adopt`, the command refuses the entry and keeps both
   copies. An identical vault copy skips this step.
5. It adds the entry to the repository's `info/exclude` when git does not already ignore
   it. It never edits a committed `.gitignore`.
6. It replaces the original with the link. A file is replaced in one rename, so it never
   goes missing. A folder is first renamed to a hidden name beside it. If the link cannot
   be made, the folder is renamed back. When the link is in place, the hidden folder is
   deleted.

A failure on one entry is reported, and the next entry still runs. The command exits 1
when any entry failed. A skill that holds installed packages is refused like this:

```
✗ ~/repos/invoice-api/.claude/skills/pdf-render/node_modules cannot be kept in the vault
  └─ A node_modules, .venv, venv, or .git folder is regenerable or a separate repository; delete or move it, adopt, then reinstall.
```

The vault ignores `node_modules`, `.venv`, and `venv`, so a linked copy does not have
them. Git records a nested repository as a reference to it, not as its files. Delete or
move the folder, adopt the entry, then install the packages again in the linked folder.

Claude Code resolves a relative `@` import in a linked file from the vault folder, where
the imported file does not exist. `--adopt` warns once for each relative import that it
finds in `CLAUDE.local.md` or in a `.md` file of a rule, and still adopts the entry. For
a rules folder, it scans each `.md` file in the folder. Write the imported content into
the file itself, or remove the import. Skills and other files are not scanned.

Claude Code treats a linked rule as an external import. The first interactive session in
a checkout shows the dialog "Allow external CLAUDE.md file imports?" with the rule's vault
path. Claude Code records the approval for that checkout path on that machine. Until you
approve it, the rule does not load, and a headless session skips it without a prompt. A
linked skill loads with no approval.

#### Adopting any path

`--adopt` also takes the path of any file or folder in the checkout. Use it for a private
file that is not `CLAUDE.local.md`, a skill, or a rule, such as a tool's config file:

```bash
sessionmemory local sync --adopt .claude/natelandau-toolkit.toml --adopt .claude/agents --cwd .
```

```
! the hooks commit the vault copy to the vault's git remote
~/repos/my-vault/projects/invoice-api/local/claude/natelandau-toolkit.toml
~/repos/my-vault/projects/invoice-api/local/claude/agents
```

A relative path starts from the shell's directory, not from `--cwd`. A path that names a
skill, a rule, or `CLAUDE.local.md`, such as `.claude/skills/deploy-check`, is adopted as
that kind of entry. A folder is linked as one entry, so a file that you add to it later
is in the vault too.

The vault cannot tell from its files where an entry adopted by path starts, so the
command lists each one in `local/entries.toml`:

```toml
# Paths adopted into this folder with local sync --adopt, relative to the
# checkout's top level. Each is linked into every checkout of the project.
paths = [
    ".claude/agents",
    ".claude/natelandau-toolkit.toml",
]
```

Every `sync` links each listed path into the checkout, and creates any missing parent
folder. If the file holds a line that the command cannot use, `sync` names the line,
links the other entries, and exits 1:

```
✗ ~/repos/my-vault/projects/invoice-api/local/entries.toml lists paths that cannot be linked
  ├─ '../notes' is not a plain path inside the checkout
  └─ Fix the file, then run local sync.
```

Of two nested lines, such as `.claude/agents` and
`.claude/agents/reviewer.md`, only the outer one is linked.

To stop linking a path, do these steps:

1. Delete its line from `entries.toml`.
2. In each checkout, delete the link. In one checkout, copy the vault copy to the path.
3. Delete the vault copy.

`--adopt` refuses a path, and names the reason, when the path:

- Is outside the work tree, or is the work tree itself.
- Is in `.git`.
- Is `.claude`, `.claude/skills`, or `.claude/rules`, or is inside a skill or a rule.
- Is `.claude/settings.json` or `.claude/settings.local.json`.
- Starts with a `claude/` folder, or is a top-level `entries.toml`. Both collide with the
  vault's layout.
- Is a top-level `settings.local.json`, which reads as the settings entry.
- Reads as a skill or rule name, such as a top-level `skills/notes`.
- Is inside a path that is already adopted, or holds one.

These names are compared without regard to case, because the file systems of macOS are
case-insensitive. The checks of [Adopting an entry](#adopting-an-entry) also apply: git
must not track the path, and a folder must not hold a symlink or an installed package
folder.

```
✗ .claude/agents/reviewer.md overlaps .claude/agents, which is already adopted
  └─ An adopted path cannot hold or sit inside another.
```

An entry adopted by path gets none of the extra handling of the other kinds. The command
does not scan it for `@` imports. The session start hook links it, but does not name it
in the note about entries that load from the next session.

> **Note:** A program that saves a file by renaming a new file over it replaces the link
> with a regular file. `local status` then shows `diverged`.

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
~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
```

Every `new` command prints the path of the file it wrote and nothing else, so
`path=$(sessionmemory new spec --title "..." --cwd .)` captures it. The command writes the
frontmatter. Write the body into that file afterwards, or pass it with `--body-file`.
`--json` prints the path, the title, and the uuid:

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

Create a spec for this project. A spec is a plain file, not a page: it has no summary
field and no size limit. `sessionmemory search --specs` finds it by an embedding of its
title, its opening paragraph, its headings, and its goal and decision sections, never by
the whole body.

| Option         | What it does                                                                  |
| -------------- | ----------------------------------------------------------------------------- |
| `--title`      | The title. Required.                                                          |
| `--body`       | Markdown body.                                                                |
| `--body-file`  | Read the body from a file, or stdin for `-`.                                  |
| `--supersedes` | The spec this one replaces, by filename. The old spec is marked superseded.   |
| `--cwd`        | Directory to resolve the project from.                                        |
| `--json`       | Emit JSON instead of prose.                                                   |

```bash
sessionmemory new spec --title "Export invoices as UBL 2.1 XML" --cwd .
```

```
~/repos/my-vault/projects/invoice-api/specs/2026-09-03-export-invoices-as-ubl-2-1-xml.md
```

The filename leads with the local creation date, so a listing of `specs/` reads in the
order the specs were written. The command writes `title`, `created`, `updated`, and
`status: draft`. After that the file is yours to edit.

A spec carries a `status` because it outlives the work it describes. The values are
`draft` (designed and not, or not fully, built), `shipped`, `superseded`, and
`abandoned`. A spec with no `status` is current. Set `status: shipped` in the file when
the work ships.

Open the body with a short paragraph directly under the `# ` title that says what the
spec is for. A search result shows the frontmatter `summary` when there is one, else the
first paragraph of a goal or purpose section, else that opening paragraph, so the opening
paragraph is the summary of any spec without either.

`--supersedes` replaces a spec. It sets the old spec's `status` to `superseded` and its
`superseded_by` to the new filename, and it leaves the old spec's body alone:

```bash
sessionmemory new spec --title "Render invoice PDFs in a worker pool" \
  --supersedes 2026-10-13-move-pdf-rendering-to-a-worker-queue --cwd .
```

```
~/repos/my-vault/projects/invoice-api/specs/2026-10-13-render-invoice-pdfs-in-a-worker-pool.md
```

The old spec's frontmatter now reads:

```yaml
status: superseded
superseded_by: 2026-10-13-render-invoice-pdfs-in-a-worker-pool.md
```

The name is the old spec's filename, with or without `.md`. A name that matches no spec
exits 1 and creates nothing. The new path is printed only after the old spec is marked;
if marking fails, the new spec is removed and the command exits 1. An old spec with no
frontmatter block gets one holding only `status` and `superseded_by`.

## `sessionmemory new plan`

Create a plan for this project. It takes the same options as `sessionmemory new spec` and
writes into `plans/`.

```bash
sessionmemory new plan --title "Move PDF rendering to a worker queue" --cwd .
```

```
~/repos/my-vault/projects/invoice-api/plans/2026-09-03-move-pdf-rendering-to-a-worker-queue.md
```

## `sessionmemory new runbook`

Create a runbook for this project: a step-by-step procedure to follow by hand. It takes
the same options as `sessionmemory new spec` and writes into `runbooks/`.

```bash
sessionmemory new runbook --title "Rotate the Stripe webhook signing secret" --cwd .
```

```
~/repos/my-vault/projects/invoice-api/runbooks/rotate-the-stripe-webhook-signing-secret.md
```

The filename is the title's slug with no date, because a runbook is followed again and
corrected in place. It follows the same naming rules as `sessionmemory new reference`
below: a title whose filename is already taken is refused, so edit the existing runbook
instead.

## `sessionmemory new reference`

Create a reference doc for this project: a private document about the project that stays
out of its repository, such as hosting settings or a data dictionary. It takes the same
options as `sessionmemory new spec` and writes into `reference/`.

```bash
sessionmemory new reference --title "Hosting" --cwd .
```

```
~/repos/my-vault/projects/invoice-api/reference/hosting.md
```

The filename is the title's slug with no date, because a reference doc is kept current by
editing it in place. A title whose filename is already taken is refused rather than given
a second file, so edit the existing doc instead:

```
✗ hosting.md already exists in ~/repos/my-vault/projects/invoice-api/reference; edit it instead
```

A long title is cut to fit the filename limit, so two different titles can produce the same
name. When the existing file holds a different title, the error names that title and asks
for another one.

## `sessionmemory new backlog`

Add one open item to this project's `backlog.md`. The command writes the full line,
dates it, and puts it under the heading for its kind. It creates the file with its
`# Backlog` heading when there is none, and adds the kind heading at the end when the
file lacks it.

| Option    | What it does                                                                                                              |
| --------- | ------------------------------------------------------------------------------------------------------------------------- |
| `--kind`  | The heading the item goes under: `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci`, or `decide`. Required. |
| `--size`  | Effort: `S`, `M`, or `L`. Required.                                                                                       |
| `--title` | The imperative description of the work. Required.                                                                         |
| `--topic` | A topic tag, without the leading hash.                                                                                    |
| `--cwd`   | Directory to resolve the project from.                                                                                    |
| `--json`  | Emit JSON instead of prose.                                                                                               |

`decide` is for a question the user has to answer before work can start, such as a choice
between two designs. It goes under a `## decide` heading and counts as an open item like
any other.

```bash
sessionmemory new backlog --kind feat --size M \
  --title "Export invoices as UBL" --topic ubl --cwd .
```

```
- [M] Export invoices as UBL - 2026-10-13 [#ubl]
~/repos/my-vault/projects/invoice-api/backlog.md
```

The command prints the line it wrote, then the path. A question for the user:

```bash
sessionmemory new backlog --kind decide --size S \
  --title "Should failed webhooks be retried?" --cwd .
```

```
- [S] Should failed webhooks be retried? - 2026-10-13
~/repos/my-vault/projects/invoice-api/backlog.md
```

Two warnings go to stderr, and neither stops the write. A topic that no other line uses
is probably a typo, so the command lists the topics in use:

```
! #credit-notes is a new topic in this backlog
  └─ in use: ubl
- [M] Add a credit note PDF - 2026-10-13 [#credit-notes]
~/repos/my-vault/projects/invoice-api/backlog.md
```

A description over 200 characters is a spec in disguise:

```
! the description is 203 characters, over the 200 limit
  └─ keep a line to one sentence and put detail in a spec
```

Both warnings print only once the line is written, so an item that is refused prints the
error alone.

With `--json`, the payload names the file and the exact line written:

```json
{
  "path": "~/repos/my-vault/projects/invoice-api/backlog.md",
  "line": "- [S] Round tax per line, not per invoice - 2026-10-13"
}
```

A kind or size outside the set is refused before anything is written:

```
Invalid value for '--kind': 'chore' is not one of 'feat', 'fix', 'refactor', 'perf',
'docs', 'test', 'build', 'ci', 'decide'.
```

So is a topic that holds whitespace or a bracket, since it would not read back as one tag:

```
✗ the topic cannot contain a bracket, which ends its [#topic] tag: 'ubl]'
```

The command only adds. A finished item, and one that will never be done, is deleted
from the file; the line has no checkbox to tick, and git history is the record of what
was finished.

## `sessionmemory log`

Record this session's work in the one page that belongs to it.

| Option         | What it does                                                                   |
| -------------- | ------------------------------------------------------------------------------ |
| `--session-id` | This session's identifier. Required.                                           |
| `--title`      | The log's title. Required unless `SESSIONMEMORY_LOG_TITLE` is set, which wins. |
| `--summary`    | One sentence a search result shows.                                            |
| `--body`       | Markdown body. Replaces what is there.                                         |
| `--body-file`  | Read the body from a file, or stdin for `-`.                                   |
| `--transcript` | Path to this session's transcript on disk.                                     |
| `--url`        | Where this session can be opened online.                                       |
| `--cwd`        | Directory to resolve the project from.                                         |
| `--json`       | Emit JSON naming the path and whether the page was created or updated.         |

```bash
sessionmemory log --session-id 0f6c1f9a-4c1e-4a1b-9d2f-6f4b2a7c5e10 \
  --title "2026-09-02 09:41" \
  --summary "Made the Stripe webhook handler idempotent." \
  --body-file - --cwd . <<'EOF'
## Summary

Added the webhook_events table and the duplicate check in the handler.
EOF
```

```
~/repos/my-vault/projects/invoice-api/logs/2026-09-02-09-41.md
```

The plugin's sweep sets `SESSIONMEMORY_LOG_TITLE` instead of passing `--title`, so the title
it computed is the one that is written:

```bash
SESSIONMEMORY_LOG_TITLE="2026-09-02 09:41" sessionmemory log \
  --session-id 0f6c1f9a-4c1e-4a1b-9d2f-6f4b2a7c5e10 --body-file - --cwd . <<'EOF'
## Summary

Added the webhook_events table and the duplicate check in the handler.
EOF
```

```
~/repos/my-vault/projects/invoice-api/logs/2026-09-02-09-41.md
```

The command prints the page's path and nothing else, as every `new` command does, so
`path=$(sessionmemory log ...)` captures it. The page is keyed on `--session-id`. A second
call for the same session updates the page it already wrote and prints the same path.
Every call replaces the summary and the whole body, so send both in full each time.
`--json` also says which of the two happened:

```bash
sessionmemory log --session-id 0f6c1f9a-4c1e-4a1b-9d2f-6f4b2a7c5e10 \
  --title "2026-09-02 09:41" \
  --summary "Made the Stripe webhook handler idempotent and documented the retry window." \
  --body-file - --json --cwd . <<'EOF'
## Summary

Added the webhook_events table and the duplicate check in the handler, then documented
how long Stripe keeps retrying.
EOF
```

```json
{
  "path": "~/repos/my-vault/projects/invoice-api/logs/2026-09-02-09-41.md",
  "action": "updated"
}
```

CAUTION: The body replaces the page's whole body. Send the complete text every time,
never a diff and never an addendum.

`--transcript` and `--url` are recorded in the page's frontmatter as `transcript` and
`session_url`, beside `session_id`. Each is written only when given, and a later call
that omits one never clears it.

## `sessionmemory search`

Find the pages nearest in meaning to the query, nearest first. This is the one read an
agent cannot do with its own tools.

| Option              | What it does                                                                                       |
| ------------------- | -------------------------------------------------------------------------------------------------- |
| `{query}`           | What to look for: a few distinctive words. Required.                                               |
| `--logs`            | Search past session logs instead of learnings.                                                     |
| `--specs`           | Search specs instead of learnings. Cannot be combined with `--logs`.                               |
| `--include-retired` | With `--specs`, also return superseded and abandoned specs. Without `--specs` it is an error.      |
| `--limit`           | Maximum number of results. Default 10.                                                             |
| `--max-distance`    | Farthest cosine distance that still counts as a hit. Default 0.45, or 0.38 with `--specs`.         |
| `--min-margin`      | How much nearer than the field's median page a hit must sit. Default 0.11, or 0.07 with `--specs`. |
| `--read`            | Print each hit's whole file under its path.                                                        |
| `--cwd`             | Directory to resolve the project from.                                                             |
| `--json`            | Emit JSON instead of prose.                                                                        |

Search with a few distinctive words, such as names, identifiers, or error text, rather
than a sentence. A paraphrase still matches, but keywords separate the page that answers
from its neighbors more clearly than a question does.

```bash
sessionmemory search "stripe duplicate webhook event"
```

```
~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
  Stripe retries a webhook for 72 hours, so the handler must be idempotent
  Stripe redelivers an unacknowledged webhook for up to 72 hours, so the handler records the event id and ignores a repeat.
```

Each result is the page's path, its title, and its summary. Read the path to get the
body, or pass `--read` to get every hit's whole file in the one call:

```bash
sessionmemory search "stripe duplicate webhook event" --read
```

```
~/repos/my-vault/projects/invoice-api/learnings/stripe-retries-a-webhook-for-72-hours-so-the-handler-must-be-idempotent.md
---
title: Stripe retries a webhook for 72 hours, so the handler must be idempotent
uuid: 0b3de896-f58b-4d49-a4be-cf356e0302d5
summary: Stripe redelivers an unacknowledged webhook for up to 72 hours, so the handler records
  the event id and ignores a repeat.
created: '2026-10-02T16:48:08Z'
updated: '2026-10-02T16:48:08Z'
---
The handler inserts the Stripe event id into `webhook_events` before doing any work. A
duplicate key means the event was already handled, and the handler returns 200 without
touching the invoice. Source: https://docs.stripe.com/webhooks#retries
```

Each page is printed as it is on disk, under its path, with a blank line between pages.

A hit is a page that stands out from the rest of its field. A query nothing answers
returns nothing rather than the nearest pages dressed up as hits:

```bash
sessionmemory search "kubernetes ingress"
```

```
no results: nothing in learnings matches; try --specs for designs and decisions, or --logs for what a past session did
```

The message names the other two places to look. A `--specs` or `--logs` search that finds
nothing names only itself, as `no results: no spec matches this query`.

A page counts as a hit when it sits within `--max-distance` of the query and at least
`--min-margin` nearer than the median page in the field. A field with fewer than 8 pages
measures against a fixed median of 0.49 instead, which makes its cutoff 0.38.

The margin is relative because a fixed distance does not separate matches from
non-matches. Phrasing a query as a question lowers every distance at once, and a session
log, which covers a whole session, sits near every query about its project. Measured on
120 labeled queries across four real projects, a fixed cutoff of 0.45 returned hits for
28 of 42 queries that had no answer. The margin returned a hit for 1 of them and missed
3 of 78 answers. `--min-margin 0` turns the margin off and leaves only `--max-distance`.

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
    "distance": 0.19797725975513458
  }
]
```

`--logs` searches the project's session logs instead:

```bash
sessionmemory search "webhook handler work" --logs --limit 2
```

```
~/repos/my-vault/projects/invoice-api/logs/2026-09-02-09-41.md
  2026-09-02 09:41
  Made the Stripe webhook handler idempotent.
```

`--specs` searches the project's specs instead, to find a decision already made before a
feature is designed again:

```bash
sessionmemory search "UBL XML export for EU customers" --specs
```

```
~/repos/my-vault/projects/invoice-api/specs/2026-10-13-export-invoices-as-ubl-2-1-xml.md
  Export invoices as UBL 2.1 XML
  Customers in the EU need each invoice as a UBL 2.1 XML file so their accounting software can import it without retyping.
```

A spec hit takes its title from the spec: the frontmatter `title`, else the first `# `
heading, else the filename. Its summary is the frontmatter `summary` when the spec has
one, else the first prose paragraph of its goal, purpose, intent, summary, or overview
sections, else the text directly under the title, else the first prose paragraph of any
section other than `Status`. The summary is cut at 300 characters on a word boundary.

A spec is searched through an embedding of its title, the text under its title, its
`summary`, its goal and purpose sections, its `## ` headings, and its decision sections,
never its whole body, and capped at 8192 bytes. A word that appears only deep in a
section can therefore not be found. Search by the topic the spec names.

A superseded or abandoned spec is left out, because the spec that replaced it is the
answer. `--include-retired` brings it back, marked `(superseded)` or `(abandoned)` after
its title:

```bash
sessionmemory search "queue for rendering PDFs" --specs --include-retired
```

```
~/repos/my-vault/projects/invoice-api/specs/2026-10-13-move-pdf-rendering-to-a-worker-queue.md
  Move PDF rendering to a worker queue (superseded)
  Rendering a large invoice PDF blocks the request thread for seconds. Move the render to a background worker queue.

~/repos/my-vault/projects/invoice-api/specs/2026-10-13-render-invoice-pdfs-in-a-worker-pool.md
  Render invoice PDFs in a worker pool
  Rendering a large invoice PDF blocks the request thread. Render in a pool of worker processes instead of a queue.
```

With `--json`, a spec hit adds its `status`, which is `draft`, `shipped`, `superseded`,
`abandoned`, or an empty string for a spec with none:

```json
[
  {
    "path": "~/repos/my-vault/projects/invoice-api/specs/2026-10-13-export-invoices-as-ubl-2-1-xml.md",
    "title": "Export invoices as UBL 2.1 XML",
    "summary": "Customers in the EU need each invoice as a UBL 2.1 XML file so their accounting software can import it without retyping.",
    "distance": 0.22222331166267395,
    "status": "draft"
  }
]
```

Specs have their own defaults, `--max-distance 0.38` and `--min-margin 0.07`, because
specs spread wide: the median distance from a query to a project's specs measured 0.53 to
0.56, so the margin below the median alone let unrelated specs through at 0.42 to 0.44,
and the absolute ceiling had to be tighter. At the learnings defaults, 3 of 10 queries
with no answer returned a spec. At 0.38 and 0.07, 0 of 10 did and none of 40 queries with
an answer was missed. Both numbers were measured on 50 queries against one project's 51
specs, so they are a starting point. An explicit `--max-distance` or `--min-margin`
overrides either one.

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

Durable memory for this project lives in a vault of markdown pages. Nothing below is
loaded for you: the titles are what the vault holds, and each is one `sessionmemory search`
away. The project's folder has `learnings/` and `logs/`, searched by meaning, beside
`specs/`, `runbooks/`, `reference/`, and `backlog.md`, which are ordinary files you Read
and Edit. `sessionmemory path <kind>` prints one path, where kind is project, learnings,
logs, specs, runbooks, reference, or backlog; `sessionmemory project --json` prints
them all for a script.

  - Before assuming nothing was written down, search: `sessionmemory search "<words>"`
    prints each hit's path, title, and summary, and `--read` prints every hit's whole
    page in one call. Search with a few distinctive words, such as names, identifiers,
    or error text, rather than a sentence. No hits means nothing is recorded, not that
    the query needs loosening.
  - Past sessions, one page each: `sessionmemory search "<words>" --logs`. Search them
    for why something was decided, what happened the last time an area changed, or
    whether a fix was already tried.
  - Open work: read `backlog.md`. Any top-level bullet is an open item. Under a
    `## <kind>` heading (feat, fix, refactor, perf, docs, test, build, ci, decide), the
    full shape is sized S, M, or L: `- [S] <imperative description> - <YYYY-MM-DD> [#topic]`.
    Add one with
    `sessionmemory new backlog --kind <kind> --size <S|M|L> --title "..." --topic <topic> --cwd .`,
    which writes that shape and creates the file or heading when missing. Questions
    for the user to answer go under `## decide`. Delete a finished line, and one
    that will never be done, directly; never tick or annotate it. Git history is the
    record of what was finished.
  - Specs: `specs/` holds one design record per feature, named `<date>-<topic>.md`
    and kept after the work ships. Before designing or changing a feature, search
    them: `sessionmemory search "<words>" --specs` (listing the directory works too), so a
    decision already made is not made again. `sessionmemory new spec --title "..." --cwd .`
    creates one and prints its path. Open every spec with a short paragraph directly
    under the `# ` title saying what it is for. A search shows the frontmatter
    `summary`, else a goal or purpose section's first paragraph, else that opening one.
    A spec carries a `status`: `draft`, `shipped`, `superseded`, or `abandoned`. Set
    `status: shipped` when its work ships; a spec with no frontmatter block gets one
    holding only `status`. Replace one with
    `sessionmemory new spec --title "..." --supersedes <old filename> --cwd .`, which marks
    the old spec superseded.
  - Runbooks: `runbooks/` holds step-by-step procedures to follow by hand, such as
    configuring a service, rotating a credential, or recovering from an outage.
    Whenever you write one, create it with `sessionmemory new runbook --title "..." --cwd .`,
    which prints its path, never in another folder or the repository.
  - Reference: `reference/` holds this project's private docs, such as hosting
    settings, where each credential lives, or a data dictionary, kept out of its
    repository. Any it holds are listed below; before working in an area, read the one
    whose name matches. Keep a doc current by editing it in place. Create one with
    `sessionmemory new reference --title "..." --cwd .`, which prints its path. Public
    documentation stays in the repository.
  - The hooks commit the vault. Do not commit it, and do not tell the user vault
    changes are left uncommitted.
  - Learnings are captured at session end, not by you mid-session. When the user asks
    you to remember something about this project, or to keep a learning now, never
    use your own memory files: `sessionmemory new learning --title "..." --summary "..." --cwd .`
    creates the page and prints the path to write prose into. Title and summary state
    the fact, not the topic. Keep a page under 8KB; more detail is another page.

## What this project knows

  - Stripe retries a webhook for 72 hours, so the handler must be idempotent

## Reference docs

  reference/hosting.md

## Runbooks

  runbooks/rotate-the-stripe-webhook-signing-secret.md

## Open work

  4 open backlog items
  3 specs in specs/
```

No page body enters the block, and the index is never read. `--command` changes the
command name the guidance uses, which is how the plugin names an absolute path for a
session with no `sessionmemory` on its `PATH`.

A `## Reference docs` section lists every markdown file in `reference/` by name, and a
`## Runbooks` section does the same for `runbooks/`. Each is left out when its folder is
empty.

A project with at least 10 learnings that was never reviewed, or was last reviewed more
than 30 days ago, gets one more line under its titles:

```
  Learnings last reviewed: never. At a natural stopping point, suggest /sessionmemory:review to the user.
```

`--json` emits the same content as nine keys: `guidance`, `project`, `titles`,
`open_backlog`, `specs`, `reference`, `runbooks`, `reviewed`, and `review_due`.
`open_backlog` and `specs` are counts, `reference` and `runbooks` are lists of filenames,
`reviewed` is the date of the last review or an empty string when never reviewed, and
`review_due` says whether the review line above is shown.

Local entries waiting in the checkout are not part of this block. The plugin's session
start reports them from `local sync` (see [SessionStart](plugin.md#sessionstart)).

## `sessionmemory reconcile`

Bring this project's session logs in line with what landed on the trunk. The command
reads the commits each log lists under `## Changes` and fixes the ones that no longer
exist. It rewrites only that section. The `## Summary` text and the log's `updated` date
stay as they are.

For each logged commit, the command decides in this order:

1. If the commit is on the trunk, it keeps the line.
2. If the `origin` remote is on GitHub, the command asks GitHub about the commit's pull
   requests. A merged pull request replaces the line with the commit that landed on the
   trunk. After a rebase merge, each commit is replaced by its own rebased copy, found by
   its patch among the trunk commits below the merge commit. If no single copy matches,
   as after a squash merge, the line becomes the merge commit. An open pull request
   leaves the line. If GitHub cannot answer, the command
   leaves the line and reports it as unchecked.
3. If a branch or a tag still holds the commit, the command leaves the line, because the
   work is in progress.
4. If `git fetch origin` failed, the command leaves the line and reports it as unchecked,
   because the trunk on `origin` can hold the commit.
5. If the repository is a shallow clone, the command leaves the line and reports it as
   unchecked, because a clone with cut-off history cannot prove the commit is gone.
6. If `origin` does not fetch every branch, as in a clone made with `--single-branch`,
   the command leaves the line and reports it as unchecked, because a branch the clone
   never fetches can hold the commit.
7. Otherwise the commit is gone, and the command removes the line.

A short sha that matches more than one commit is left as it is and reported as unchecked.
When a logged commit is not on the local trunk, the command runs `git fetch origin` before
it decides. It does not fetch when every logged commit is on the local trunk, or when
the repository has no `origin`. The fetch is the only change the command makes to the
repository. The `gh` command is needed only when `origin` is on GitHub. The command
exits 1 if the project has no absolute registered root, if the root is not the top-level
directory of a git repository, or if git cannot answer a question.

| Option      | What it does                              |
| ----------- | ----------------------------------------- |
| `--cwd`     | Directory to resolve the project from.    |
| `--dry-run` | Report what would change without writing. |
| `--json`    | Emit JSON instead of prose.               |

```bash
sessionmemory reconcile --dry-run --cwd .
```

```
✓ (dry run) reconciling
  └─ log-commits: 1 log changed, 0 replaced, 1 dropped, 0 in progress, 0 unchecked
```

The same run without `--dry-run` prints the same counts and rewrites the log.

```bash
sessionmemory reconcile --json --dry-run --cwd .
```

```json
[
  {
    "reconciler": "log-commits",
    "changed": [
      "~/repos/my-vault/projects/invoice-api/logs/2026-10-01-09-00.md"
    ],
    "replaced": 0,
    "dropped": 1,
    "in_progress": 0,
    "unchecked": 0,
    "notes": []
  }
]
```

`changed` lists the logs whose commit list changes, also on a dry run. `notes` holds the
reason for each unchecked line.

## `sessionmemory reindex`

Bring this project's learnings, logs, and specs indexes up to date with the files beside
them, or every project's with `--all`.

| Option   | What it does                            |
| -------- | --------------------------------------- |
| `--cwd`  | Directory to resolve the project from.  |
| `--all`  | Reindex every project in the vault.     |
| `--json` | Emit JSON instead of prose.             |

```bash
sessionmemory reindex --cwd .
```

```
✓ reindexing
  ├─ learnings: 1 added, 0 updated, 0 removed, 0 unchanged
  ├─ logs: 0 added, 0 updated, 0 removed, 0 unchanged
  └─ specs: 3 added, 0 updated, 0 removed, 0 unchanged
```

`sessionmemory search` does the same refresh on every call, so this command is for doing
that work up front, or for rebuilding an index file you deleted.

```json
{
  "learnings": {
    "added": 0,
    "updated": 0,
    "removed": 0,
    "unchanged": 1
  },
  "logs": {
    "added": 0,
    "updated": 0,
    "removed": 0,
    "unchanged": 0
  },
  "specs": {
    "added": 0,
    "updated": 0,
    "removed": 0,
    "unchanged": 3
  }
}
```

`--all` walks every project folder in the vault instead of resolving one from `--cwd`,
so it runs from any directory and reaches a project whose repository is not on this
machine. It is the one command to run after cloning a vault, or when `sessionmemory
doctor` reports more than one index behind its pages. `--all` and `--cwd` are mutually
exclusive.

```bash
sessionmemory reindex --all
```

```
✓ reindexing
  ├─ invoice-api/learnings: 0 added, 0 updated, 0 removed, 1 unchanged
  ├─ invoice-api/logs: 0 added, 0 updated, 0 removed, 0 unchanged
  └─ invoice-api/specs: 0 added, 0 updated, 0 removed, 3 unchanged
```

A page whose bytes have not changed is skipped, so a run over a healthy vault costs
almost nothing. A folder left behind by a project that was unregistered is walked too,
since it still holds pages.

With `--json`, the payload nests each field's counts under its project:

```json
{
  "invoice-api": {
    "learnings": {
      "added": 0,
      "updated": 0,
      "removed": 0,
      "unchanged": 1
    },
    "logs": {
      "added": 0,
      "updated": 0,
      "removed": 0,
      "unchanged": 0
    },
    "specs": {
      "added": 0,
      "updated": 0,
      "removed": 0,
      "unchanged": 3
    }
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

See [Vault health](vault-health.md) for the eleven checks and what to do about each one.

## `sessionmemory export`

Write this project's field as a `.memoryfield.zip`, the format's archival transport.

| Option     | What it does                                                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `--logs`   | Export the logs field instead of learnings.                                                                                     |
| `--output` | Where to write the zip. Defaults to `<slug>.memoryfield.zip` in this directory, or `<slug>-logs.memoryfield.zip` with `--logs`. |
| `--cwd`    | Directory to resolve the project from.                                                                                          |
| `--json`   | Emit JSON instead of prose.                                                                                                     |

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

| Variable                    | What it does                                                                      |
| --------------------------- | --------------------------------------------------------------------------------- |
| `SESSIONMEMORY_VAULT`       | Where the vault is. Wins over `vault.root` in `~/.claude/sessionmemory.toml`.     |
| `SESSIONMEMORY_MODEL_CACHE` | Where the embedding model is cached. Defaults to `~/.cache/sessionmemory/models`. |
| `SESSIONMEMORY_EMBEDDER`    | Test only. `stub` selects a hash-derived embedder and loads no model.             |

## What is not built

- **No cross-project search.** A page belongs to one project and is reachable from that
  project. There is no command that searches the whole vault.
- **No global pages.** Knowledge that applies everywhere belongs in a skill or in a
  repository's own `CLAUDE.md`.
- **No ranking or limit on injection.** A session receives every title. When a project's
  list grows too long to read, the answer is deleting pages.
- **No `--fix`.** `sessionmemory doctor` reports and never repairs.
