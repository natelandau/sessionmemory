---
name: backlog
description: Triage and curate this project's backlog.md - validate the open items against the current repository, remove or amend them, then rank what remains by impact and effort to recommend what to do next.
disable-model-invocation: true
argument-hint: "[--yes]"
---

# Vault Backlog

Triage this project's `backlog.md` and report what is worth doing next. Two
read-only reviewer subagents judge the items. You apply their verdicts to the file
and write the report. You are the only writer.

The arguments to this invocation are `$ARGUMENTS`. Without `--yes`, this skill
stops and asks the user before it applies a low-confidence `CLOSE` or `REMOVE`.
With `--yes`, it applies those verdicts too and lists them in the report.

## Step 1: locate the backlog

```bash
BACKLOG="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --backlog)"
```

If the command exits non-zero, no vault is reachable. Say so and stop. If the file
does not exist, report "No backlog found for this project." and stop.

An item is one line under a `## <kind>` heading:

```
- [S|M|L] <description> - <YYYY-MM-DD> [#topic]
```

The line has no checkbox. A finished item is deleted, and git history is the
record of it.

## Step 2: clean up lines with a checkbox

The session start does not count a line that has a checkbox. Find those lines
before you dispatch anything:

```bash
grep -nE '^- \[[ x]\]' "$BACKLOG"
```

- Delete every `- [x]` line. Its author marked it done.
- Rewrite every `- [ ] [S|M|L] ...` line as `- [S|M|L] ...`. Keep the rest of the
  line. Treat the line as open from here on.

Apply both with Edit and without asking. List both in the report.

## Step 3: list the open items

```bash
grep -nE '^- \[[SML]\]' "$BACKLOG"
```

If no line is open, report "The backlog has no open items." and stop.

Pass lines to the reviewers verbatim. Each reviewer keys its answers by the line
text.

## Step 4: dispatch the reviewers

Both reviewers run on Sonnet. Give each dispatch a batch of about 5 lines. It
returns one result per line. Run the batches in parallel. If there are only a few
lines, use one batch.

### Validate the open items

Dispatch `backlog-validity-reviewer` with the path of `backlog.md` and one batch
of open lines. It returns one verdict per line, keyed by `item`, with cited
`evidence` and a `confidence`:

| Verdict  | Meaning                                                                      |
| -------- | ---------------------------------------------------------------------------- |
| `CLOSE`  | The work is done.                                                            |
| `REMOVE` | The item is obsolete.                                                        |
| `AMEND`  | The item is real, but the line has drifted. `proposed_change` holds the fix. |
| `KEEP`   | The item is real and accurate as written.                                    |

`KEEP` and `AMEND` items are open.

### Score the open items

Dispatch `backlog-opportunity-reviewer` with the same path and the `KEEP` and
`AMEND` lines. It returns `impact`, `effort`, `recommend_now`, and a `reason` per
line, keyed by `item`.

## Step 5: apply the verdicts

Every verdict is an Edit to `backlog.md`. Git history keeps all of them.

- `CLOSE`: delete the line.
- `REMOVE`: delete the line.
- `AMEND`: rewrite the description or the size, or move the line under the correct
  `## <kind>` heading. Keep the date.
- `KEEP`: leave the line.

If a `CLOSE` or `REMOVE` has low confidence and `--yes` was not passed, ask the
user before you apply it.

This skill never adds an item and never edits the learnings field.

## Step 6: report

1. Changes applied. Give the counts in one line. Then list each closed and removed
   line with its evidence, and mark each one that `--yes` applied on low
   confidence. Then list each ticked line you deleted and each checkbox line you
   rewrote.
2. Open backlog at a glance. Give the count, then a table of the open lines by kind
   and size.
3. Work on next. List the open lines where `recommend_now` is yes, best first, each
   with its impact, effort, and the one-line reason. If no line clears the bar, say
   so.
