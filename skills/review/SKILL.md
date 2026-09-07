---
name: review
description: Curate this project's learnings - sharpen weak titles and summaries, retire stale, historical, or redundant pages, and route a page that is really deferred work into backlog.md. Read-only reviewer subagents judge. This skill applies the changes.
disable-model-invocation: true
---

# Vault Review

Curate this project's learnings field. The end-of-session sweep adds and refines
pages. This pass keeps the field accurate, non-redundant, and at the right
altitude. A page's title is what every session start shows, and its summary is
what a search returns. A weak title or summary is a memory that in effect does not
exist. Fixing those is the most valuable work this skill does.

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
backlog line outside the item shape, and a stale index. Three of those have a
fixed remedy. Apply it before you dispatch a reviewer:

- Rename a nonconformant filename with `mv`.
- Split an oversized page into two.
- If the index is stale, run `"$CLI" reindex --cwd .`.

Leave a `backlog` finding alone. The `backlog` skill clears it. Mention it in the
report.

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

Write one short paragraph or a brief list: pages reviewed, titles and summaries
sharpened, pages merged and deleted, backlog lines added, and each CLAUDE.md
recommendation, stated as not applied. Then list every `DELETE` the reviewer
returned with low confidence, each with its one-line reason, so a wrong one can be
reverted from the vault's history.
