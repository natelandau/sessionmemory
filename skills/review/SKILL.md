---
name: review
description: Curate this project's vault knowledge - sharpen weak titles and summaries, retire stale or redundant learnings, and resolve backlog items the repository shows are done. Read-only reviewer subagents judge; this skill applies the changes.
disable-model-invocation: true
---

# Vault Review

Curate this project's persisted knowledge: the learnings field and `backlog.md`.
The end-of-session sweep adds and refines; this pass is what keeps the field
accurate, non-redundant, and at the right altitude. Since a page's title is what
every session start shows and its summary is what a search returns, a weak title
or summary is a memory that effectively does not exist, and fixing those is the
most valuable thing this skill does.

You orchestrate read-only reviewer subagents and apply their verdicts. You are the
only writer here.

## Locate what to review

```bash
CLI="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --cli)"
LEARNINGS="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --learnings)"
BACKLOG="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --backlog)"
```

A non-zero exit means no vault is reachable; say so and stop.

## Health check

```bash
"$CLI" doctor
```

It reports a page with a nonconformant filename, a page over 8KB, unparsable
frontmatter, a dead project root, or a stale index. Fix a filename with `mv`,
split an oversized page into two, and run `"$CLI" reindex` for a stale index,
all before dispatching reviewers.

## Identify what to review

```bash
ls "$LEARNINGS"/*.md
grep -n '^- \[ \]' "$BACKLOG"
```

If there is nothing in either, report that and stop.

## Dispatch the reviewers

Each reviewer runs on Sonnet in its own context, takes a **batch**, and returns
one verdict per item, judged independently. Group into batches of roughly 5, one
agent per batch, run in parallel; a single batch when there are only a handful.

- **`memory-entry-reviewer`**: batch the learning filenames. Pass the learnings
  directory and the filenames. It returns, per page, `KEEP`/`UPDATE`/`DELETE`
  with a cited reason, a `proposed_title` and `proposed_summary` whenever either
  is weak, a `proposed_change` for the body on UPDATE, a `backlog_candidate` for
  a page that really names deferred work, and a `claude_md_candidate`. Match by
  `target`.
- **`backlog-validity-reviewer`**: batch the open lines. Pass the path of
  `backlog.md` and the lines verbatim. It returns `CLOSE`/`REMOVE`/`AMEND`/`KEEP`
  per line, keyed by `item`.
- **`redundancy-reviewer`**: once, over the whole learnings directory. It returns
  clusters of overlapping pages naming a `merge_target`.

The `backlog-opportunity-reviewer` is `backlog`'s job. Do not dispatch it here.

## Apply changes

- **Titles and summaries**: apply every `proposed_title` and `proposed_summary`
  with Edit, on the frontmatter lines only. These are the highest-value edits in
  the pass.
- **Merges**: fold each cluster into its `merge_target` with Edit, then
  `"$CLI" delete` the others.
- **Learnings**: `UPDATE` rewrites the body in place; `DELETE` runs
  `"$CLI" delete "$LEARNINGS/<file>"`; `KEEP` does nothing.
- **Backlog**: `CLOSE` changes the line's `- [ ]` to `- [x]`; `REMOVE` deletes
  the line; `AMEND` edits the line's text, size, or section; `KEEP` does nothing.
- **Backlog routing** (from `backlog_candidate`): dedupe against the open lines,
  then add one line under the right `## <kind>` section in the format
  `- [ ] [S] <item> - <today> [#topic]`. A `workaround` page stays; a
  `superseded` page is deleted like any other DELETE.
- **CLAUDE.md promotion** (from `claude_md_candidate`): recommend only, never
  apply. Surface the reason and the suggested entry; do not edit `CLAUDE.md` and
  do not delete the page.

Neither a delete nor an edit needs a confidence gate: the vault is a git
repository and a wrong change is recovered from its history. Confirm a
low-confidence backlog `CLOSE` with the user before ticking it.

## Report

One short paragraph or a brief list: pages reviewed, titles and summaries
sharpened, pages merged and deleted; backlog lines closed, removed, amended, or
routed in; any CLAUDE.md recommendations, stated plainly as not applied.
