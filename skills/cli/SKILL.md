---
name: cli
description: Use when writing a spec, a plan, or a learning that should outlive this session, or when looking for knowledge an earlier session in this repository recorded. Covers the project-memory vault CLI - searching this project's pages by meaning, creating a page, and finding where this project's files live.
---

# Vault CLI

Durable memory for this repository lives in its own folder of a vault: a flat
`learnings/` field of markdown pages, a `logs/` field with one page per session,
`specs/`, `plans/`, and a `backlog.md` checklist. The CLI does only what you cannot
do with Read, Grep, and Write: it searches pages by meaning, it creates pages, and
it writes a backlog item in the one shape the session start counts.

Resolve the CLI once, then use `$CLI` for every command:

```bash
CLI="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --cli)"
```

If it exits non-zero, there is no vault on this machine. Say so and carry on;
nothing here is required to work.

## Before assuming, search

```bash
"$CLI" search "connection pooling"          # this project's learnings, by meaning
"$CLI" search "connection pooling" --read   # every hit's whole page, in one call
"$CLI" search "the deploy that failed" --logs   # past sessions
"$CLI" search "ruff" --json                 # path, title, summary, distance
```

A result is a path. Read it, or pass `--read` to get every hit's page at once.
`search` ranks by meaning, so a paraphrase still finds a hit, and it refreshes
the index first, so a page written a moment ago is already findable. A query
nothing answers returns no results rather than the nearest pages; that is the
answer, not a reason to loosen `--max-distance`.

## Where this project's files live

```bash
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --project    # the folder
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --learnings  # the learnings field
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --specs
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --plans
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --backlog    # backlog.md
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --logs
```

`backlog.md`, specs, and plans are ordinary files. Read and edit them directly;
the CLI only adds to them.

## Creating a page

A learning is created by the CLI, which owns the filename, the uuid, and the
dates, and never by the Write tool:

```bash
"$CLI" new learning --title "..." --summary "..." --cwd . --body-file - <<'EOF'
The whole page, however long, with no shell-quoting to get wrong.
EOF
```

The title is what every future session sees at start and the summary is what a
search returns, so both state the fact, not the topic. Keep a page under 8KB;
more detail is a second page.

Do not create a learning mid-session unless the user asked for one: a learning is
a judgment about what mattered, and that judgment is only sound in hindsight. The
end-of-session sweep makes it.

Specs and plans are files with a title and dates:

```bash
"$CLI" new spec --title "..." --cwd . --body-file - <<'EOF'
...
EOF
"$CLI" new plan --title "..." --cwd .
```

A backlog item is one line under a `## <kind>` heading, sized S, M, or L, with
today's date and a topic tag. The CLI writes it, creating the file or the heading
when either is missing, so the line is always the shape the session start counts:

```bash
"$CLI" new backlog --kind feat --size S --title "cache the model between reindex runs" --topic index --cwd .
```

Kind is one of feat, fix, refactor, perf, docs, test, build, ci. Ticking a finished
item and deleting one that will never be done are direct edits to `backlog.md`.

## Deleting

A page that is wrong or spent is deleted. There is no status to move it to; the
vault's git history is the undo.

```bash
"$CLI" delete <path> [<path>...]
```

## Registering a project

A project has to be registered before it can hold pages. Nothing else is decided
at registration:

```bash
"$CLI" project --register --cwd .
"$CLI" project --cwd . --json     # every path, once registered
```
