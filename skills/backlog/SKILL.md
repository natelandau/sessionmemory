---
name: backlog
description: Triage and curate this project's backlog.md - validate the open items against the current repository, tick, remove, or amend them, then rank what remains by impact and effort to recommend what to do next.
disable-model-invocation: true
---

# Vault Backlog

Triage this project's `backlog.md` and surface what is worth doing next. Two
read-only reviewer subagents do the analysis: one checks whether each open item is
still real, the other scores the real ones by value. You apply the resulting edits
and report. You are the only writer here.

## Locate the backlog

```bash
BACKLOG="$("${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --backlog)"
grep -n '^- \[ \]' "$BACKLOG"
```

A non-zero exit from the resolver means no vault is reachable; say so and stop.
If the file does not exist, report "No backlog found for this project." and stop.
If no line is open, report "The backlog has no open items." and stop.

An item is one line: `- [ ] [S|M|L] <description> - <date> [#topic]`, under a
`## <kind>` heading. Pass lines to the reviewers verbatim; they key their answers
by the line text.

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

- **`CLOSE`**: change the line's `- [ ]` to `- [x]`.
- **`REMOVE`**: delete the line.
- **`AMEND`**: rewrite the line's description, size, or move it to the right
  `## <kind>` section. Keep the date.
- **`KEEP`**: leave it.

Confirm a low-confidence `CLOSE` or `REMOVE` with the user first. This skill never
adds an item and never touches the learnings field.

## Report

1. **Changes applied**: one line with the counts, then the closed and removed
   lines with their evidence.
2. **Open backlog at a glance**: a count, then a table of open lines by kind and
   size.
3. **Work on next**: the open lines where `recommend_now` is yes, best first, each
   with impact, effort, and the one-line reason. If nothing clears the bar, say so.
