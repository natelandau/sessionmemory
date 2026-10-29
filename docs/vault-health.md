# Vault health

`sessionmemory doctor` reads every project in the vault and reports what is off. It never
edits a file, and it always exits 0. If a check stops with an error, `doctor` reports it
as a `doctor` finding that names the check, and runs the other checks.

```bash
sessionmemory doctor
```

```
8 suggestions
  ├─ filename: ~/repos/my-vault/projects/invoice-api/learnings/Bad Name.md: not lowercase ascii letters, digits, and hyphens
  ├─ size: ~/repos/my-vault/projects/invoice-api/learnings/an-oversized-page.md: 10044 bytes; split it, the limit is 8192
  ├─ datetime: ~/repos/my-vault/projects/invoice-api/learnings/an-undated-page.md: created, updated: unquoted; quote the value
  ├─ project: old-service: root ~/repos/old-service does not exist
  ├─ backlog: ~/repos/my-vault/projects/invoice-api/backlog.md: line 15: ticked; finished work is deleted, not ticked
  ├─ reference: ~/repos/my-vault/projects/invoice-api/specs/2026-10-13-move-pdf-rendering-to-a-worker-queue.md: line 8: names runbooks/deploy-the-worker.md, which does not exist
  ├─ spec: ~/repos/my-vault/projects/invoice-api/specs/2026-10-13-export-invoices-as-ubl-2-1-xml.md: status 'wip' is not one of draft, shipped, superseded, abandoned
  └─ index: ~/repos/my-vault/projects/invoice-api/learnings: index is behind its pages; run: sessionmemory reindex
```

The vault path is shortened to `~` here; the command prints absolute paths.

A clean vault says so:

```
✓ nothing to report
```

`--json` prints the same findings as a list of `check`, `path`, and `message`:

```json
[
  {
    "check": "filename",
    "path": "~/repos/my-vault/projects/invoice-api/learnings/Bad Name.md",
    "message": "not lowercase ascii letters, digits, and hyphens"
  }
]
```

## The eleven checks

Each finding names the check that produced it.

### `filename`

A markdown file in a field or in `specs/` carries a name outside the format's alphabet:
lowercase ASCII letters, digits, and hyphens, starting and ending with a letter or a
digit.

Rename the file. Until then it is not a page: it is never indexed, never searched, and
never listed at session start. A spec with such a name is never searched by
`sessionmemory search --specs` and never counted at session start.

### `size`

A page is larger than 8192 bytes. One page is one embedding, and only the first 8192
bytes reach the model, so the tail of the page is invisible to search.

Split it into two pages. Each one gets its own title and summary, which is what search
and injection show.

### `frontmatter`

A page's frontmatter cannot be parsed, or a learnings page has no `title` or no
`summary`, or the file is not valid UTF-8.

Open the file and correct the block. A learnings page needs both fields: the title is
what a session sees at its start, and the summary is what a search result shows, so a
page missing either one is memory nobody can find. A logs page with no frontmatter block
at all is fine and is not reported.

### `datetime`

A page's frontmatter carries a bare date or datetime, one written without quotes.

```
1 suggestion
  └─ datetime: ~/repos/my-vault/projects/invoice-api/learnings/an-undated-page.md: created, updated: unquoted; quote the value
```

Quote the value: `created: '2026-09-02T10:00:00Z'`. The format requires it because a YAML
1.1 parser types a bare datetime and a YAML 1.2 parser leaves it a string, so what the
page says would depend on which one reads it. Every page this CLI writes is quoted; a
page edited by hand can lose the quotes. A block that cannot be parsed at all is
reported by the `frontmatter` check instead.

### `project`

A registered project records a repository root that no longer exists.

```
1 suggestion
  └─ project: old-service: root ~/repos/old-service does not exist
```

If the repository moved, edit that entry's `root` in `_system/registry.toml`. If the
repository is gone for good, delete the entry, and delete the project's folder under
`projects/` when you want its pages gone too.

This check also reports a `registry` finding when `_system/registry.toml` cannot be read
at all. Correct the file by hand: with the registry unreadable, no directory resolves to
a project and every other command fails.

### `backlog`

A top-level bullet in a project's `backlog.md` is ticked as `- [x]`. Every other
top-level bullet with text counts as an open item at session start, whether or not it
carries a size, a date, or a topic.

```
1 suggestion
  └─ backlog: ~/repos/my-vault/projects/invoice-api/backlog.md: line 15: ticked; finished work is deleted, not ticked
```

A ticked `- [x]` line is finished work. Delete it: the backlog has no checkbox, and git
history is the record of what was done. You can also run `/sessionmemory:backlog`, which
deletes ticked lines before it triages. An indented bullet is a note under an item and is
not reported.

### `reference`

A spec, runbook, reference doc, or learning names a vault file that does not exist, such
as `runbooks/deploy-the-worker.md` or `specs/2026-09-03-export-invoices.md`. Only a
mention that starts the line or follows whitespace, a backtick, a quote, `(`, `[`, or `<`
counts, and the mention ends at `.md`, so a repository path like `docs/specs/x.md`, a
placeholder like `specs/<slug>.md`, and a longer extension like `specs/x.mdx` do not.

```
1 suggestion
  └─ reference: ~/repos/my-vault/projects/invoice-api/specs/2026-10-13-move-pdf-rendering-to-a-worker-queue.md: line 8: names runbooks/deploy-the-worker.md, which does not exist
```

Either the file was renamed or deleted, or it was never written. Correct the mention to
the real name, write the missing page, or delete the sentence that points at it. A mention
of a plan is never reported, since plans are scratch and may be gone by design.

### `spec`

A spec's `status` or `superseded_by` is not usable, or its frontmatter cannot be read.
This run covers a vault holding one spec of each kind and one healthy spec:

```
5 suggestions
  ├─ spec: ~/repos/my-vault/projects/invoice-api/specs/2026-10-13-cache-tax-rates-per-region.md: superseded_by '2026-10-13-no-such-spec.md' names no spec in specs/
  ├─ spec: ~/repos/my-vault/projects/invoice-api/specs/2026-10-13-export-invoices-as-ubl-2-1-xml.md: status 'wip' is not one of draft, shipped, superseded, abandoned
  ├─ spec: ~/repos/my-vault/projects/invoice-api/specs/2026-10-13-move-pdf-rendering-to-a-worker-queue.md: superseded without superseded_by
  ├─ spec: ~/repos/my-vault/projects/invoice-api/specs/2026-10-13-retry-failed-pdf-renders.md: superseded_by names this spec
  └─ spec: ~/repos/my-vault/projects/invoice-api/specs/2026-10-13-send-payment-reminders.md: frontmatter cannot be parsed; status ignored
```

- **`status 'wip' is not one of ...`**: the status is not `draft`, `shipped`,
  `superseded`, or `abandoned`. Change it to the one that fits, or delete the key, since a
  spec with no status counts as current.
- **`superseded without superseded_by`**: the spec says a newer spec replaced it and does
  not name which. Add `superseded_by: <filename>`, or run
  `sessionmemory new spec --supersedes <old>` next time, which writes both keys.
- **`superseded_by '...' names no spec in specs/`**: the named file was renamed, deleted,
  or mistyped. Correct the name to the real file.
- **`superseded_by names this spec`**: the spec names itself as its replacement. Name the
  spec that replaced it.
- **`frontmatter cannot be parsed; status ignored`**: the frontmatter block is not valid
  YAML, so its `status` is never read and the spec counts as current. Fix the block. Until
  then, search leaves the whole block out of what it embeds and shows, and finds the spec
  from its `# ` title, its opening text, and its headings.

A spec with `superseded_by` set and no `status: superseded` is not reported. A spec with
no frontmatter block at all is current and is not reported.

### `commits`

A session log lists a commit in its `## Changes` section that no local branch,
remote-tracking ref, or tag contains. Squashing or rebasing a branch replaces its commits,
so the log then names commits that no longer exist. One finding names the log and counts
those commits:

```
1 suggestion
  └─ commits: ~/repos/my-vault/projects/invoice-api/logs/2026-10-20-ubl-export.md: lists 1 commit no branch contains; run: sessionmemory reconcile
```

Run `sessionmemory reconcile --dry-run` to see how many lines it would replace or drop,
then run `sessionmemory reconcile` to rewrite the lists (see
[`reconcile`](cli.md#sessionmemory-reconcile)).

The check reads only the local repository. It never fetches and never calls GitHub. It skips
a project whose registered root is missing or is not the top-level directory of a
repository, and a project where git fails. A short sha that matches more than one object is not counted, because the commit can
be on the trunk. It also skips a shallow clone, because a commit can be older than the
clone's history, and a clone whose `origin` does not fetch every branch, as one made with
`--single-branch`, because a commit can be on a branch the clone never fetches. In both
cases `reconcile` leaves such commits unchecked.

### `index`

A field's index file, or the one in `specs/`, cannot be opened as a database, or it
disagrees with the files beside it.

Run `sessionmemory reindex --cwd <your repo>`, or `sessionmemory reindex --all` when more
than one index is reported. The index is derived and disposable, so deleting the
`*.sqlite3` file in the folder and reindexing has the same effect.
`sessionmemory search` refreshes the index it queries, so this finding usually means a
field nobody has searched since its pages changed.

### `local`

A file in a project's `local/` folder has a problem, or a checkout's link to it does. The
check runs for each project, and for each entry that the vault holds (see
[Local files](concepts.md#local-files)).

A `CLAUDE.local.md` directly in the project's vault folder, outside `local/`, is at its
old path:

```
1 suggestion
  └─ local: ~/repos/my-vault/projects/invoice-api/CLAUDE.local.md: CLAUDE.local.md is at its old path; run local sync to move it into local/
```

Run `sessionmemory local sync`. It moves the file into `local/`. If `local/` already holds
a different copy, the finding says so:

```
1 suggestion
  └─ local: ~/repos/my-vault/projects/invoice-api/CLAUDE.local.md: CLAUDE.local.md is at its old path and differs from ~/repos/my-vault/projects/invoice-api/local/CLAUDE.local.md; keep one of them, delete the other, then run local sync
```

Keep the copy you want, delete the other one, then run `sessionmemory local sync`.

An entry in `local/` that does not have the shape of its kind is never linked. Examples
are a file among the skills, which must be folders, and a folder at
`local/CLAUDE.local.md`:

```
1 suggestion
  └─ local: ~/repos/my-vault/projects/invoice-api/local/claude/skills/notes.md: skills/notes.md in the vault is not a folder; fix or remove it in the vault
```

Move the file into a skill folder, or delete it. Until then, `sessionmemory local sync`
reports a conflict for it and exits 1.

A skill or rule folder in the vault that holds a `.git` folder is a separate repository.
Git records it in the vault as a reference to that repository, not as its files, so a
second machine gets an empty folder:

```
1 suggestion
  └─ local: ~/repos/my-vault/projects/invoice-api/local/claude/skills/deploy-check/vendor/.git: skills/deploy-check holds a git repository, which the vault commits as an embedded repository rather than its files; move it out of the vault
```

Move the repository out of the vault, and clone it again where you need it.
`sessionmemory local adopt` refuses a folder like this.

`local/entries.toml` lists the paths that you adopted by name. A listed path with no
vault copy cannot be linked:

```
1 suggestion
  └─ local: ~/repos/my-vault/projects/invoice-api/local/claude/review.toml: .claude/review.toml is listed in entries.toml but the vault holds no copy; remove the line from entries.toml
```

Delete the line from `entries.toml`, or adopt the file again from a checkout that still
has it.

A file or folder in `local/` that no entry names is never linked. The check reports the
highest such path once, not each file inside it:

```
1 suggestion
  └─ local: ~/repos/my-vault/projects/invoice-api/local/notes: notes in the vault is not an entry; adopt it again from a checkout, or add its path to entries.toml
```

Add its checkout path to `entries.toml`, or delete it. A concurrent `local adopt`
in two sessions can drop one path from `entries.toml` and leave its copy behind like
this. The check also reports an `entries.toml` that is not valid TOML, a `paths` value
that is not a list of strings, and each line that names a path that `local adopt` refuses.

`local/declined.toml` lists the entries that you declined. The check reports a file that
is not valid TOML, and each line that is not an entry name. At the registered root, it
also reports a declined entry that the checkout does not hold:

```
1 suggestion
  └─ local: ~/repos/my-vault/projects/invoice-api/local/declined.toml: skills/deploy-check is declined in declined.toml but this checkout has no copy; remove the line if no other checkout has it
```

If no other checkout or machine holds the entry, delete the line. The line does no harm
if you keep it. The check never deletes it, because the entry can still exist in a
checkout that the check cannot see.

A relative `@` import in `CLAUDE.local.md` or in a `.md` file of a rule cannot load,
because Claude Code resolves it from the vault folder:

```
1 suggestion
  └─ local: ~/repos/my-vault/projects/invoice-api/local/CLAUDE.local.md: @./sandbox-notes.md resolves from the vault folder and will not load
```

Write the content of the imported file into the file itself, or remove the import. Skills
and the other files of a rule are not scanned.

When the registered root exists on this machine, the check also looks at each entry
there. It reports at most one finding for each entry, with the remedy that fits its state.

An entry that git tracks belongs to the project, and `sessionmemory local sync` leaves it
alone:

```
1 suggestion
  └─ local: ~/repos/invoice-api/.claude/skills/release-notes: skills/release-notes is tracked by git; run git rm --cached .claude/skills/release-notes before adopting it
```

If the entry is yours, run the command, commit the removal in the project, then run
`sessionmemory local adopt skills/release-notes`. The files stay in the project's earlier
commits. If the project means to share the entry, delete the vault copy instead.

A real file or folder whose content differs from the vault copy is `diverged`:

```
1 suggestion
  └─ local: ~/repos/invoice-api/.claude/rules/style.md: rules/style.md differs from the vault copy; copy the newer one over the vault copy, delete the checkout copy, and run local sync
```

An editor that saves by renaming a temporary file over the link leaves this state behind.
Run `sessionmemory local status` to compare both copies. Then copy the newer content over
the vault copy, delete the checkout copy, and run `sessionmemory local sync`.

A link to nothing is reported whether or not git ignores it:

```
1 suggestion
  └─ local: ~/repos/invoice-api/.claude/skills/deploy-check: skills/deploy-check is a dangling symlink; run local sync to replace it
```

Run `sessionmemory local sync`, which replaces the link.

The other findings are for a link or a copy that git does not ignore, which shows up as an
untracked file in the project. The remedy depends on what is there:

```
1 suggestion
  └─ local: ~/repos/invoice-api/CLAUDE.local.md: CLAUDE.local.md is not ignored by git; run local sync to add it to info/exclude
```

The checkout links to the vault copy. Run `sessionmemory local sync`, which adds the entry
to the file that `git rev-parse --git-path info/exclude` prints.

```
1 suggestion
  └─ local: ~/repos/invoice-api/.claude/rules/style.md: rules/style.md is not ignored by git; run local adopt rules/style.md to replace it with an ignored link
```

The checkout holds a real copy with the same content as the vault copy. Run the
`sessionmemory local adopt` command that the finding names, which replaces the copy with a
link and adds the ignore line.

```
1 suggestion
  └─ local: ~/repos/invoice-api/.claude/rules/style.md: rules/style.md is not ignored by git, and a symlink to ~/repos/notes/style.md rather than the vault copy; remove it, then run local sync
```

The checkout holds a link to another file. Remove the link, then run
`sessionmemory local sync`.

When `.claude/`, `.claude/skills/`, or `.claude/rules/` is itself a symlink or a file, no
entry under it can be linked, and the finding says so. Replace it with a real folder.

The checkout's `.claude/settings.local.json` is reported when the two copies need your
choice:

```
1 suggestion
  └─ local: ~/repos/invoice-api/.claude/settings.local.json: settings.local.json differs from the vault copy; run local sync to choose which copy to keep
```

Run `sessionmemory local sync` and keep one copy. The same remedy applies when only the
checkout or only the vault has a copy. A settings file that is a symlink is reported
because Claude Code cannot write through it: replace it with a regular file. A settings
file that git tracks is reported with the `git rm --cached` command that keeps it private.
When `.claude/` is a symlink or not a folder, or a copy is a folder or a symlink, the
settings cannot be synced and the finding says so. Replace it with a real folder or a
regular file.

The check covers the registered root only. `sessionmemory local status` covers each
worktree.

## Why every finding is a suggestion

Semantic search never surfaces a page that does not match, so an imperfect page costs
nothing but the disk it sits on. Nothing here justifies failing a build, and a check that
failed one would fail it forever.

There is no `--fix`. Each finding above forks on a judgment the command cannot make: a
new filename, where to split a page, what a missing summary says, whether a repository
moved or died. Repair that guessed at any of those would destroy the signal that made the
finding worth reporting. The ticked backlog line is the one with a fixed remedy, and
`/sessionmemory:backlog` applies it, since curating that file is what the skill is for.
