# Vault health

`sessionmemory doctor` reads every project in the vault and reports what is off. It never
edits a file, and it always exits 0.

```bash
sessionmemory doctor
```

```
5 suggestions
  ├─ filename: ~/repos/my-vault/projects/invoice-api/learnings/Bad Name.md: not lowercase ascii letters, digits, and hyphens
  ├─ size: ~/repos/my-vault/projects/invoice-api/learnings/an-oversized-page.md: 9764 bytes; split it, the limit is 8192
  ├─ datetime: ~/repos/my-vault/projects/invoice-api/learnings/an-undated-page.md: created, updated: unquoted; quote the value
  ├─ project: old-service: root ~/repos/old-service does not exist
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

## The six checks

Each finding names the check that produced it.

### `filename`

A markdown file in a field carries a name outside the format's alphabet: lowercase ASCII
letters, digits, and hyphens, starting and ending with a letter or a digit.

Rename the file. Until then it is not a page: it is never indexed, never searched, and
never listed at session start.

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

### `index`

A field's index file cannot be opened as a database, or it disagrees with the pages
beside it.

Run `sessionmemory reindex --cwd <your repo>`. The index is derived and disposable, so
deleting the `*.sqlite3` file in the field and reindexing has the same effect.
`sessionmemory search` refreshes the index it queries, so this finding usually means a
field nobody has searched since its pages changed.

## Why every finding is a suggestion

Semantic search never surfaces a page that does not match, so an imperfect page costs
nothing but the disk it sits on. Nothing here justifies failing a build, and a check that
failed one would fail it forever.

There is no `--fix`. Each finding above forks on a judgment the command cannot make: a
new filename, where to split a page, what a missing summary says, whether a repository
moved or died. Repair that guessed at any of those would destroy the signal that made the
finding worth reporting.
