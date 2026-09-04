# Concepts

A vault is a directory of markdown, one folder per project, plus a vector index that the
markdown alone can rebuild. This page describes what is in it.

## The vault is markdown

Every durable thing this tool keeps is a markdown file you can read, edit, move, and
delete with any editor. The CLI creates pages and searches them by meaning. It does no
other work on your files, because an agent already reads, greps, and writes markdown
well, and it cannot embed text.

Make the vault a git repository. Git is what recovers a page you deleted, and the plugin
commits the vault when a session starts and again when a session ends.

## Vault layout

```
<vault>/
  _system/vault.toml                 the marker that identifies a vault
  _system/registry.toml              repo path -> project name
  projects/<name>/
    learnings/                       a field
      <slug>.md                      pages
      nomic-embed-text-v1.5.sqlite3  vector index (gitignored, derived)
    logs/                            a second field, one page per session
      <date>-<time>.md
      nomic-embed-text-v1.5.sqlite3
    specs/<date>-<slug>.md           plain files, not a field, never indexed
    plans/<date>-<slug>.md
    backlog.md                       a checklist, not a page
```

`learnings/` and `logs/` are fields: flat directories of pages, each with its own
index file. The format forbids indexing a page in a sub-directory, so `specs/`, `plans/`,
and `backlog.md` sit outside both fields. That is what keeps a spec or a checklist from
being embedded as memory.

A project's files are found by its slug and nothing else. There is no global folder, and
no page belongs to more than one project.

## A page

A page is markdown with five frontmatter fields. Three of them are mechanical, and the
CLI writes them. The other two, `title` and `summary`, are the judgments, and each one
carries real weight: the title is what a session sees at its start, and the summary is
what a search result shows.

```markdown
---
title: Tests suppress INP001 per-file; tests/__init__.py would break pytest imports
uuid: f9f4b530-4c7c-414f-a994-cde3ff352e87
summary: Test files suppress INP001 with per-file noqa comments rather than adding tests/__init__.py,
  which would change how pytest imports the suite.
created: '2026-08-14T21:09:50Z'
updated: '2026-08-14T21:09:50Z'
---
The `tests/` directory intentionally has no `__init__.py`. Adding one is ruff's
own suggested fix for INP001 ("file is part of an implicit namespace package"),
but it changes pytest's import mode in ways that can break test discovery and
import resolution for this suite.

Each test file instead carries a file-level `# ruff: noqa: INP001` comment.
New test files must include this comment to stay lint-clean.

## Read when

- adding a new test file under tests/
- seeing INP001 violations in test files
- considering whether to add tests/__init__.py
```

The body is prose, and every heading in it is prose too. The `## Read when` section above
is a convention the author of that page chose. Nothing in the CLI reads it. Cite the file,
the commit, or the URL a fact came from wherever one exists.

Keep a page under 8192 bytes. One page is one embedding with no chunking, and only the
first 8192 bytes reach the model, so the tail of a longer page is invisible to search.
More detail is a second page, never a longer one. `sessionmemory doctor` reports a page
over the limit.

A filename is ASCII lowercase letters, digits, and hyphens, and it starts and ends with a
letter or a digit. `sessionmemory new learning` derives the name from the title and takes
the first free one, so two pages written with the same title get two files. A page you add
by hand under any other name is reported by `sessionmemory doctor` and is never indexed.

## Specs, plans, and the backlog

These three sit beside the fields and are never embedded. `sessionmemory new spec` and
`sessionmemory new plan` write `title`, `created`, and `updated`, and nothing else, and
name the file for its local creation date and title, as `2026-09-03-export-invoices-as-ubl.md`.
After that they are ordinary files, so edit them directly.

`backlog.md` is a checklist. Each open item is one line, grouped under a heading for its
commit type:

```markdown
# Backlog

## feat

- [ ] [M] cache the embedding model between reindex runs - 2026-09-02 [#index]

## docs

- [x] [S] document the five doctor checks - 2026-09-01 [#doctor]
```

The size is `S`, `M`, or `L`. The heading is one of `feat`, `fix`, `refactor`, `perf`,
`docs`, `test`, `build`, or `ci`. `sessionmemory new backlog` writes an item in exactly
that shape, dated, under its heading, and creates the file or the heading when either is
missing. Finished work becomes `- [x]` on its existing line, and work that will never
happen is deleted; both are edits to the file. A session start reports how many `- [ ]`
lines the file holds, and nothing more.

## Logs

`logs/` is a second field holding one page per session. The end-of-session pass
writes it, keyed on the session id, and it replaces the page's whole body on every call.
The page is titled for the date and clock time the session began, and the filename is
dated for the day the title names, so a session that crosses midnight is filed under the
day it started and two sessions on one day get two distinct names.
A long session is swept more than once, so each sweep resends everything the last one
wrote. The page's frontmatter names the transcript on disk as `transcript` and, when
the session was reachable from claude.ai, its link as `session_url`, so a reader can
go from a log to the conversation behind it.

Logs are never injected into a session. Search them on request:

```bash
sessionmemory search "why did we reject ollama" --logs
```

## Deletion replaces status

A page that is wrong or spent is deleted. There is no `draft`, no `active`, no
`superseded`, and no `archived`, so nothing has to be moved through a state machine and
nothing accumulates as a tombstone. Git holds the history, `sessionmemory delete` removes
the page and its index row together, and the plugin's next commit records the removal.

## The index

Each field carries one SQLite file named for the embedding model that filled it, such as
`nomic-embed-text-v1.5.sqlite3`. It holds one row per page: the filename, the
frontmatter, the modification time, the sha256 of the file, and the embedding of the
whole file, frontmatter included.

The index is derived and disposable. It is gitignored, it is never backed up, and
deleting it costs nothing:

```bash
rm ~/repos/my-vault/projects/invoice-api/learnings/*.sqlite3
sessionmemory reindex --cwd ~/repos/invoice-api
```

Freshness is the sha256 of each file, so a page edited in any editor is re-embedded on the
next read. Every `sessionmemory search` refreshes the field's index before it queries,
which means a page written moments ago is found without anyone running
`sessionmemory reindex` first. `sessionmemory reindex` exists for the case where you want
that work done up front.

Changing the model changes the index filename, so two models never share a file and no
migration is needed.

## What a session receives

`sessionmemory inject` prints one block: a fixed guidance section that says how to read
and write the vault, then the title of every learning the project holds, then a count of
open backlog items with the titles of any specs and plans. No summaries, no bodies, no
triggers.

Titles rather than summaries, because injection is a push channel and must not grow into
the thing it is trying to save. For a project with 62 pages, titles cost roughly 400
tokens where titles and summaries would cost about 2,500.

Titles rather than nothing, because an agent does not reliably decide to search. An agent
that hits a failing test starts debugging, and it has no reason to believe that a past
session wrote anything down. Pushing titles trades recall for recognition: the agent
never has to generate the idea of searching, and only has to notice that a title matches
its situation. It is a weak guarantee, since it depends on a title happening to match,
and it is accepted as one.

Automatic prompt-time retrieval is deferred rather than rejected. A hook that embedded
each prompt and injected the nearest pages would remove the agent's decision entirely. The
threshold such a hook needs is already measured and applied: `sessionmemory search`
returns only pages within a cosine distance of 0.45, the point past which the nearest page
to an unrelated query sits, and `--json` prints each hit's distance.

## The memoryfield format

The pages follow the memoryfield format by Cal Paterson, described in
[his article](https://calpaterson.com/memoryfields.html) and defined in
[the memoryfield spec](https://github.com/calpaterson/memoryfield-spec). A field built
here satisfies the spec's requirements: a flat directory, conformant filenames, an index
named for the model code, and the whole file as the embedding input. `sessionmemory export`
writes a `.memoryfield.zip` that other tools reading the format can consume.

Index timestamps are the file modification time. The spec's committer-date rule covers a
field received through git, and these fields are read as local directories.

One deviation from the format is deliberate. **The vector index is not committed.** The
spec says a field carries one. Here it is gitignored and rebuilt, because committing a
768-dimension float32 blob for every page would churn git history on every write.
`sessionmemory export` builds a fresh index into the zip, so a distributed field does
carry one.

`index.md` and `listing.md` are optional, and this vault writes neither. That is a choice
rather than a deviation. Nothing here is distributed as a field with an introduction, and
`sessionmemory inject` pushes titles over a separate channel rather than cataloguing them
in a file.

Two more places differ from the design spec for this vault, and the code is the
authority: the registry file is `_system/registry.toml` rather than `projects.toml`, and
injection lists spec and plan titles and a count of open backlog items beside the
learning titles.

## See also

- [CLI reference](cli.md) for every command and its output.
- [The Claude Code plugin](plugin.md) for what writes pages automatically.
- [Vault health](vault-health.md) for what `sessionmemory doctor` reports.
