---
name: review
description: Curate this project's learnings - sharpen weak titles and summaries, retire stale, historical, or redundant pages, route a page that is really deferred work into backlog.md, and bring log commit lists in line with the trunk. Read-only reviewer subagents judge. This skill applies the changes.
disable-model-invocation: true
---

# Vault Review

Curate this project's learnings field. The end-of-session sweep adds and refines
pages. This pass keeps the field accurate, non-redundant, and at the right
altitude. A page's title is what every session start shows, and its summary is
what a search returns. A weak title or summary is a memory that in effect does not
exist. Fixing those is the most valuable work this skill does.

The pass also reconciles the project's session logs: it replaces or drops logged
commits that no longer exist, so a log lists only commits that landed.

The `backlog` skill validates and ranks the open backlog items. This skill edits
`backlog.md` only to add a line for a learning that is deferred work.

You dispatch read-only reviewer subagents and apply their verdicts. You are the
only writer. Nothing here waits on the user. The vault is a git repository, and a
wrong change is recovered from its history.

## Step 1: locate the field

```bash
CLI="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --cli)"
LEARNINGS="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --learnings)"
BACKLOG="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --backlog)"
```

If a command exits non-zero, no vault is reachable. Say so and stop.

## Step 2: run the health check

```bash
"$CLI" doctor
```

It reports a page with a nonconformant filename, a page over 8KB, frontmatter that
does not parse, a bare date in frontmatter, a project root that does not exist, a
ticked backlog line (`- [x]`), a mention of a vault file that does not exist, a
stale index, and a log that lists commits no branch contains. Four of those have a
fixed remedy. Apply it before you dispatch a reviewer:

- Rename a nonconformant filename with `mv`.
- Split an oversized page into two.
- If the index is stale, run `"$CLI" reindex --cwd .`.
- If a log lists commits no branch contains (a `commits` finding), run
  `"$CLI" reconcile --cwd .`. Step 2a does this.

Leave a `backlog` finding alone. The `backlog` skill clears it. Leave a `reference`
finding alone too, since the right fix depends on what the page meant. Mention both in
the report.

## Step 2a: reconcile logs

```bash
"$CLI" reconcile --cwd . --json
```

The JSON is a list with one object per reconciler, named by its `reconciler` field.
Each object has `changed` (a list of the paths of the logs rewritten, so its length is
the number of logs changed), `replaced`, `dropped`, `in_progress`, `unchecked`, and
`notes`. Keep the payload, and report each reconciler's counts under its name. If the
command exits non-zero, keep its error message for the report and go on. The learnings
review does not depend on it.

## Step 3: list the pages

```bash
ls "$LEARNINGS"/*.md
```

If there is no page, report that and stop.

## Step 4: dispatch the reviewers

Each reviewer runs on Sonnet in its own context. It takes a batch and returns one
verdict per item, judged independently. Group the pages into batches of about 5,
one dispatch per batch, and run the batches in parallel. If there are only a few
pages, use one batch.

`memory-entry-reviewer` takes the learnings directory and one batch of filenames.
It returns one object per page, keyed by `target`:

- `verdict`: `KEEP`, `UPDATE`, or `DELETE`, with a cited reason and a
  `confidence`.
- `value`: `pass` or `fail`, naming the mistake a future agent would make without
  the page and what it would cost, or why none exists. A page that fails is
  `DELETE`.
- `proposed_title` and `proposed_summary`: present whenever the existing one is
  weak.
- `proposed_change`: the corrected body, on `UPDATE`.
- `backlog_candidate`: present when the page names deferred work, with `item`,
  `kind`, `size`, and `learning_role`.
- `claude_md_candidate`: present when the page belongs in the project's
  `CLAUDE.md`, with a `reason` and a `suggested_entry`.

`redundancy-reviewer` runs once over the whole learnings directory. It returns
clusters of overlapping pages, each with its `files` and a `merge_target`.

Do not dispatch the two backlog reviewers here. They belong to the `backlog`
skill.

## Step 5: apply the verdicts

Apply every verdict. No delete and no edit needs a confidence gate.

- Titles and summaries: apply every `proposed_title` and `proposed_summary` with
  Edit, on the frontmatter lines only. These are the highest-value edits in the
  pass.
- Merges: fold each cluster into its `merge_target` with Edit. Then delete the
  other files with `"$CLI" delete`.
- `UPDATE`: rewrite the body in place with Edit.
- `DELETE`: run `"$CLI" delete "$LEARNINGS/<file>"`.
- `KEEP`: do nothing.
- Backlog routing, from `backlog_candidate`: first make sure that no open line in
  `$BACKLOG` already covers the item. Then add it:

  ```bash
  "$CLI" new backlog --kind <kind> --size <S|M|L> --title "<item>" --topic <topic> --cwd .
  ```

  The command writes the line under the correct `## <kind>` heading. A page whose
  `learning_role` is `workaround` stays. A page whose `learning_role` is
  `superseded` is deleted, the same as any other `DELETE`.
- CLAUDE.md promotion, from `claude_md_candidate`: recommend only. Report the
  reason and the suggested entry. Do not edit `CLAUDE.md`, and do not delete the
  page.

## Step 6: report

Write one short paragraph or a brief list. For the logs, report the logs changed (the
length of `changed`), replaced, dropped, in progress, and unchecked, then every note
verbatim, or the error message if `reconcile` failed. For the learnings, report pages
reviewed, titles and summaries sharpened, pages merged and deleted, backlog lines
added, and each CLAUDE.md recommendation, stated as not applied. Then list every `DELETE` the reviewer
returned with low confidence, each with its one-line reason, so a wrong one can be
reverted from the vault's history.

## Step 7: record the review

After the report, record today's date as this project's last review, so the
session start stops nudging for one:

```bash
"$CLI" project mark-reviewed --cwd .
```

Say in the report that you did.
