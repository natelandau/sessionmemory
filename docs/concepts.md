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
    specs/<date>-<slug>.md           plain files, indexed for search --specs, not a field
      nomic-embed-text-v1.5.sqlite3  vector index (gitignored, derived)
    plans/<date>-<slug>.md           scratch (gitignored)
    runbooks/<slug>.md
    reference/<slug>.md
    backlog.md                       a list of open work, not a page
    local/                           private files that checkouts link to or copy
      CLAUDE.local.md
      entries.toml                   the paths you adopted by name
      declined.toml                  the entries you declined to adopt
      claude/skills/<name>/
      claude/rules/<name>.md
```

`learnings/` and `logs/` are fields: flat directories of pages, each with its own
index file. `specs/` has an index file of the same kind, but it is not a field. A spec has
no summary field and no size limit, and the index embeds a derived text rather than the
file (see [The index](#the-index)). `plans/`, `runbooks/`, `reference/`, `backlog.md`, and
`local/` are never indexed, which is what keeps the backlog or a plan from being embedded
as memory.

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

## Specs, plans, runbooks, reference docs, and the backlog

These sit beside the fields. Only specs are embedded, and only for `search --specs`.
`sessionmemory new spec`, `sessionmemory new plan`, `sessionmemory new runbook`, and
`sessionmemory new reference` write `title`, `created`, and `updated`, and a spec also
gets `status: draft`. After that they are ordinary files, so edit them directly.

A spec or plan records work at the time it was designed, so its file is named for its
local creation date and title, as `2026-09-03-export-invoices-as-ubl.md`. A plan is
scratch for the work it describes, so the vault's `.gitignore` and the plugin's commits
both keep `plans/` out of git. A spec is committed like any other file.

A spec carries a `status`, because it outlives the work it describes: `draft` (designed
and not, or not fully, built), `shipped`, `superseded`, or `abandoned`. A spec with no
`status` is current. `sessionmemory new spec --supersedes <old>` writes the replacement
and marks the old spec `superseded`, with `superseded_by` naming the new file.
`sessionmemory search --specs` leaves superseded and abandoned specs out unless asked
with `--include-retired`.

A search result shows a spec's summary: its frontmatter `summary`, else the first prose
paragraph of its goal or purpose section, else the paragraph directly under the `# `
title. Open every spec with a short paragraph there that says what the spec is for. For
a spec with neither a `summary` nor a goal section, that paragraph is what a search shows
and what a session decides by.

A runbook is a step-by-step procedure to follow by hand, such as configuring a service or
rotating a credential. It has its own folder so a procedure someone will run again is not
lost among plans for work that has already shipped.

A reference doc is a private document about the project that does not belong in its
repository, such as hosting settings, where each credential lives, or a data dictionary.
Public documentation stays in the repository, where it changes with the code it describes.

Runbooks and reference docs are named for their title alone, as `hosting.md`. Both are
edited in place for as long as they are true, so a date in the name would be wrong after
the first edit. A title whose name is already taken is refused rather than given a second
file, since two copies of one living doc drift apart.

`backlog.md` is a list of open work. Every top-level bullet with text is an open item,
whether or not it carries a size, a date, or a topic. The full shape, which
`sessionmemory new backlog` writes, is grouped under a heading for its commit type:

```markdown
# Backlog

## feat

- [M] cache the embedding model between reindex runs - 2026-09-02 [#index]

## decide

- [S] Should failed webhooks be retried? - 2026-09-02
```

The size is `S`, `M`, or `L`. The heading is one of `feat`, `fix`, `refactor`, `perf`,
`docs`, `test`, `build`, `ci`, or `decide`. `## decide` holds questions the user has to
answer before work can start, so a session sees them as a decision to ask for and not as
a task to begin. `sessionmemory new backlog` writes an item in the full shape, dated,
under its heading, and creates the file or the heading when either is missing. There is
no checkbox: finished work is deleted from the file, the same as work that will never
happen, and git history is the record of what was done. A session start reports how many
bullets the file holds, and nothing more. An indented bullet is a note under an item and
is not counted. A ticked `- [x]` line is not an open item either, and
`sessionmemory doctor` reports it.

## Local files

Some Claude Code files belong to you and not to the project: a `CLAUDE.local.md` with a
sandbox URL, which account to use, or where a credential lives, a skill that only you run,
a rule for how you like to work, and the permissions and plugins you approved for the
project. They must not reach the project's repository, so they
normally exist in one clone only. A second machine, a fresh clone, and a new git worktree
start without them.

The vault keeps one copy of each in the project's `local/` folder:

| Entry                 | In the checkout               | In the vault                       |
| --------------------- | ----------------------------- | ---------------------------------- |
| `CLAUDE.local.md`     | `CLAUDE.local.md`             | `local/CLAUDE.local.md`            |
| `skills/<name>`       | `.claude/skills/<name>/`      | `local/claude/skills/<name>/`      |
| `rules/<name>`        | `.claude/rules/<name>`        | `local/claude/rules/<name>`        |
| A path you adopt      | `.claude/agents`              | `local/claude/agents`              |
| `settings.local.json` | `.claude/settings.local.json` | `local/claude/settings.local.json` |

The vault folder is `claude/`, with no dot, so Finder, Obsidian, and `ls` show it. For
every entry except `settings.local.json`, each checkout holds an absolute symlink to the
vault copy. Claude Code loads a link as it
loads a regular file, and an edit made through Claude Code changes the vault copy. The
vault's remote carries the copies to your other machines. Nothing in `local/` is a page,
and nothing indexes, lists, or searches it. Record where a credential lives, never the
credential, because the vault is committed.

Each skill and each rule is linked on its own, not the whole `.claude/skills/` or
`.claude/rules/` folder. Git cannot track a file behind a symlinked folder, so per-entry
links are what let a skill that the project commits sit beside your private ones. A skill
or rule that git tracks is the project's, and the `local` commands leave it alone.

`sessionmemory local adopt` moves existing files into the vault. The plugin then
links every other checkout at session start. `sessionmemory local decline` keeps an
entry out of the vault for good. The vault records the name, so no checkout of the
project offers that entry again. Git ignores each link through the repository's
`info/exclude` file, which all worktrees of a clone share, so a link never reaches a
commit of the project.

`.claude/settings.local.json` is copied, not linked. Claude Code refuses to write through
a symlinked settings file, and it writes this file itself whenever you approve a
permission or install a plugin at local scope. So the checkout's copy and the vault copy
can each be the newer one. Nothing copies one over the other until you choose: at session
start the agent shows you what each copy has that the other lacks and asks which one to
keep. The whole copy you choose replaces the other. The two are never merged.

Three limits come from how Claude Code loads these files:

- A relative `@` import, such as `@./notes.md`, resolves from the vault folder, where the
  imported file does not exist. Claude Code also asks you to approve the import as an
  external file. Put the content into the file itself, or remove the import.
  `local adopt` and `doctor` report each relative import in `CLAUDE.local.md` and in the
  `.md` files of a rule.
- Claude Code treats a linked rule as an external import. It loads only after you approve
  the dialog "Allow external CLAUDE.md file imports?" once in each checkout. Until then a
  headless session skips the rule. A linked skill needs no approval.
- Claude Code reads its memory files and settings before `SessionStart` runs. A link that
  the hook creates loads from the next session. For `CLAUDE.local.md`, the hook adds the
  file's content to the injected block in the session that created the link. After you
  keep the vault's settings copy, start a new session before you approve anything.

A project folder does not need to be a git repository. A workspace folder that holds
several repositories side by side can be a registered project, and its local files work
the same way. A folder inside a project that is a git repository or a registered project
is its own project, so no entry of the outer project goes inside it. Claude Code reads a
`CLAUDE.local.md` from each parent folder, so a session inside such a repository also
loads the workspace's file, as it loads a `CLAUDE.md` there.

The commands find the `CLAUDE.local.md` at the project's root, and the entries directly
inside `.claude/skills/` and `.claude/rules/`, without being told. The root is the top
level of the git work tree, or the registered root of a folder outside git. Any other
private file or folder, such as a tool's config file in `.claude/`, becomes an entry when
you adopt it by its path. Its vault copy keeps the same path, with `.claude/` stored as
`claude/`. A folder holds no record of where an entry inside it starts, so
`local/entries.toml` lists each path adopted this way, and every checkout links the same
paths. See [`sessionmemory local`](cli.md#sessionmemory-local) for the commands.

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

A page that is wrong or spent is deleted. A page has no `draft`, no `active`, no
`superseded`, and no `archived`, so nothing has to be moved through a state machine and
nothing accumulates as a tombstone. Git holds the history, `sessionmemory delete` removes
the page and its index row together, and the plugin's next commit records the removal.

Specs are the exception. A spec is kept after its work ships, because it records why the
work took the shape it has, so a spec carries a status that says whether it still
describes the system.

## The index

Each field, and `specs/`, carries one SQLite file named for the embedding model that
filled it, such as `nomic-embed-text-v1.5.sqlite3`. It holds one row per file: the
filename, the frontmatter, the modification time, the sha256 of the file, and an
embedding. For a page the embedding is of the whole file, frontmatter included.

For a spec it is not. Specs average 14KB and reach 40KB, and only the first 8192 bytes
fit one embedding, so a spec embeds a derived text instead: its title, the text under its
title, its `summary`, its goal and purpose sections, its `## ` headings joined by `; `,
and its decision sections, cut at the last whole line within 8192 bytes. The body is never
embedded. Search only has to land on the right spec, and a reader opens the file for the
detail. A spec is found by its title and headings, which a file name cannot carry once a
topic crosses several features.

The index is derived and disposable. It is gitignored, it is never backed up, and
deleting it costs nothing:

```bash
rm ~/repos/my-vault/projects/invoice-api/learnings/*.sqlite3
sessionmemory reindex --cwd ~/repos/invoice-api
```

```
✓ reindexing
  ├─ learnings: 1 added, 0 updated, 0 removed, 0 unchanged
  ├─ logs: 0 added, 0 updated, 0 removed, 0 unchanged
  └─ specs: 0 added, 0 updated, 0 removed, 3 unchanged
```

Freshness is the sha256 of each file, so a page edited in any editor is re-embedded on the
next read. Every `sessionmemory search` refreshes the index it queries first, which means
a page written moments ago is found without anyone running `sessionmemory reindex`.
`sessionmemory reindex` exists for the case where you want that work done up front, and
`sessionmemory reindex --all` does it for every project in the vault at once, which is the
whole restore procedure after a `git clone`.

Changing the model changes the index filename, so two models never share a file and no
migration is needed.

## What a session receives

`sessionmemory inject` prints one block: a fixed guidance section that says how to read
and write the vault, then the title of every learning the project holds, then the name of every reference
doc and every runbook, then a count of open backlog items and a count of specs. No
summaries, no bodies, no triggers.

Specs are counted rather than listed because a spec outlives the work it describes. A
list of them is a changelog rather than open work, and it grows with every feature the
project ever shipped. The count says whether listing `specs/` is worth a call, and the
guidance says when to make it: before designing or changing a feature, since the file
names are dated topic slugs and a matching one holds a decision already made. Plans are
not mentioned at all. The tool that writes them manages them.

Reference docs are listed by name, because they are few and are edited rather than
accumulated. A session working on a deploy that sees `reference/hosting.md` knows to read
it, which a count would not tell it. Runbooks are listed for the same reason: a session
that is about to rotate a credential should see that a procedure for it exists.

When a project holds at least 10 learnings and was never reviewed, or was last reviewed
more than 30 days ago, one line under the titles suggests running `/sessionmemory:review`.
The guidance also tells a session how to find a path (`sessionmemory path <kind>`), that
a request to remember something goes through `sessionmemory new learning` and never the
agent's own memory files, and that the hooks commit the vault.

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
returns only pages within a cosine distance of 0.45 that also sit at least 0.11 nearer to
the query than the median page in their field, the margin an unrelated query's nearest
page does not reach. A field with fewer than 8 pages measures against a fixed median of
0.49 instead. `--json` prints each hit's distance.

Specs have their own cutoff, 0.38 and 0.07. Specs spread wide: the median distance from a
query to a project's specs measured 0.53 to 0.56, so the margin alone let unrelated specs
through at 0.42 to 0.44, and the absolute ceiling had to be tighter. At the learnings
cutoff, 3 of 10 queries with no answer returned a spec. The two numbers were measured on
50 queries against one project's specs, and they are the cutoff that missed none of the 40
answered queries and returned a spec for none of the 10 unanswered ones.

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
injection carries a count of open backlog items and a count of specs beside the learning
titles.

## See also

- [CLI reference](cli.md) for every command and its output.
- [The Claude Code plugin](plugin.md) for what writes pages automatically.
- [Vault health](vault-health.md) for what `sessionmemory doctor` reports.
