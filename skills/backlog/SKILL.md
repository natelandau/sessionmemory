---
name: backlog
description: Triage and curate this project's backlog.md - validate the open items against the current repository, remove or amend them, then rank what remains by impact and effort to recommend what to do next.
disable-model-invocation: true
argument-hint: "[--yes]"
---

# Vault Backlog

Triage this project's `backlog.md` and surface what is worth doing next. Two
read-only reviewer subagents do the analysis: one checks whether each open item is
still real, the other scores the real ones by value. You apply the resulting edits
and report. You are the only writer here.

The invocation's arguments are `$ARGUMENTS`. This skill stops to confirm a
low-confidence `CLOSE` or `REMOVE`; `--yes` applies those too and lists them in
the report instead, for a run nobody is watching.

## Locate the backlog

```bash
BACKLOG="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --backlog)"
grep -nE '^- \[[SML]\]' "$BACKLOG"
```

A non-zero exit from the resolver means no vault is reachable; say so and stop.
If the file does not exist, report "No backlog found for this project." and stop.

An item is one line: `- [S|M|L] <description> - <date> [#topic]`, under a
`## <kind>` heading. There is no checkbox: a finished item is deleted, and git
history is the record of it. Pass lines to the reviewers verbatim; they key their
answers by the line text.

## Clear finished and misshapen lines first

Before dispatching anything, look for lines the session start does not count:

```bash
grep -nE '^- \[[ x]\]' "$BACKLOG"
```

Delete every `- [x]` line: its author already marked it done. Rewrite every
`- [ ] [S|M|L] ...` line to `- [S|M|L] ...`, keeping the rest of it, and treat it
as open from here on. Both are Edits, applied without asking, and both are listed
in the report. Then re-run the first grep. If no line is open, report "The backlog
has no open items." and stop.

Both reviewers run on Sonnet, take a batch of about 5 lines, and return one result
per line. Run batches in parallel; a single batch when there are only a handful.

## Phase 1: validate the open items

Dispatch `backlog-validity-reviewer` with the path of `backlog.md` and each batch
of open lines. It returns `CLOSE` (done), `REMOVE` (obsolete), `AMEND` (real but
drifted), or `KEEP`, with cited evidence and a `confidence`, keyed by `item`.
Treat `KEEP` and `AMEND` as genuinely open.

## Phase 2: score the genuinely-open items

Dispatch `backlog-opportunity-reviewer` with the same path and the genuinely-open
lines. It returns `impact`, `effort`, `recommend_now`, and a reason, keyed by `item`.

## Phase 3: apply the verdicts

Every disposition is an Edit to `backlog.md`; git history keeps all of them.

- **`CLOSE`**: delete the line.
- **`REMOVE`**: delete the line.
- **`AMEND`**: rewrite the line's description, size, or move it to the right
  `## <kind>` section. Keep the date.
- **`KEEP`**: leave it.

Confirm a low-confidence `CLOSE` or `REMOVE` with the user first, unless `--yes`
was passed. This skill never adds an item and never touches the learnings field.

## Report

1. **Changes applied**: one line with the counts, then the closed and removed
   lines with their evidence, marking each one applied under `--yes` on low
   confidence, then any ticked line deleted and any checkbox line rewritten.
2. **Open backlog at a glance**: a count, then a table of open lines by kind and
   size.
3. **Work on next**: the open lines where `recommend_now` is yes, best first, each
   with impact, effort, and the one-line reason. If nothing clears the bar, say so.
