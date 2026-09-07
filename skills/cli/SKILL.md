---
name: cli
description: Use when writing a spec, a plan, or a learning that should outlive this session, or when looking for knowledge an earlier session in this repository recorded. Covers the project-memory vault CLI - searching this project's pages by meaning, creating a page, and finding where this project's files live.
---

# Vault CLI

This repository's durable memory lives in its own folder of a vault. The folder
holds:

- `learnings/`, a flat field of markdown pages, searched by meaning.
- `logs/`, a field with one page per past session, searched by meaning.
- `specs/` and `plans/`, ordinary files.
- `backlog.md`, the list of open work.

The CLI does only what Read, Grep, and Write cannot: it searches pages by meaning,
it creates pages, and it writes a backlog item in the one shape the session start
counts.

## Resolve the CLI once

```bash
CLI="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --cli)"
```

Use `$CLI` for every command below. If the command exits non-zero, there is no
vault on this machine. Say so and continue with your task. Nothing here is
required.

## Search before you assume

```bash
"$CLI" search "connection pooling"              # this project's learnings, by meaning
"$CLI" search "connection pooling" --read       # every hit's whole page, in one call
"$CLI" search "the deploy that failed" --logs   # past sessions
"$CLI" search "ruff" --json                     # path, title, summary, distance per hit
```

Each hit is a path. Read it, or pass `--read` to get every hit's page in one call.
`search` ranks by meaning, so a paraphrase still matches. It refreshes the index
before it queries, so a page written a moment ago is already found. If nothing
matches, `search` returns no results rather than the nearest pages. No results
means nothing is recorded. Do not loosen `--max-distance`.

## Find this project's files

The path resolver prints one absolute path per call:

```bash
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --project
```

| Flag          | Prints                     |
| ------------- | -------------------------- |
| `--project`   | the project's vault folder |
| `--learnings` | the learnings field        |
| `--logs`      | the logs field             |
| `--specs`     | the specs folder           |
| `--plans`     | the plans folder           |
| `--backlog`   | `backlog.md`               |

`backlog.md`, specs, and plans are ordinary files. Read and Edit them directly.
The CLI only adds to them.

## Create a learning

The CLI creates every learning. It owns the filename, the uuid, and the dates.
Never create a learning with the Write tool.

```bash
"$CLI" new learning --title "..." --summary "..." --cwd . --body-file - <<'EOF'
The whole page, however long, with no shell quoting to get wrong.
EOF
```

Every future session sees the title at start, and a search returns the summary.
Both must state the fact, not the topic. Keep a page under 8KB. More detail is a
second page.

Do not create a learning mid-session unless the user asks for one. A learning is a
judgment about what mattered, and that judgment is only sound in hindsight. The
end-of-session sweep makes it.

## Create a spec or a plan

A spec and a plan are files with a title and dates:

```bash
"$CLI" new spec --title "..." --cwd . --body-file - <<'EOF'
...
EOF
"$CLI" new plan --title "..." --cwd .
```

## Add a backlog item

A backlog item is one line under a `## <kind>` heading. It carries a size of S, M,
or L, today's date, and a topic tag. The CLI writes the line, and it creates the
file or the heading when either is missing, so the line always has the shape the
session start counts:

```bash
"$CLI" new backlog --kind feat --size S --title "cache the model between reindex runs" --topic index --cwd .
```

`--kind` is one of feat, fix, refactor, perf, docs, test, build, or ci.

Delete a finished item from `backlog.md` directly. Delete an item that will never
be done the same way. The line has no checkbox to tick. Git history is the record
of what was finished.

## Delete a page

Delete a page that is wrong or spent. There is no status to move it to. The
vault's git history is the undo.

```bash
"$CLI" delete <path> [<path>...]
```

## Register a project

A project must be registered before it can hold pages. Registration decides
nothing else:

```bash
"$CLI" project --register --cwd .
"$CLI" project --cwd . --json     # every path, once registered
```
