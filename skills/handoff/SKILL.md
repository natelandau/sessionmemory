---
name: handoff
description: Write a consume-once handoff of the current session's working state into this project's vault, so the next session picks up where this one left off. Most useful right before /compact or /clear. The next fresh session (compact, clear, or startup) injects the handoff and then deletes it.
disable-model-invocation: true
---

# Vault Handoff

Capture where work stands **right now** into a `HANDOFF.md` in this project's
machine-local state. This is a transient baton, not durable memory, so it never reaches
the vault: SessionStart injects it into the next fresh session and immediately deletes
it. The end-of-session sweep handles durable learnings separately, so a handoff is for
the in-flight task you are about to carry across a `/compact`, a `/clear`, or a new
session.

## Locate the handoff file

Run the path resolver to get the absolute target path. It derives the project's
state directory for you, so never re-derive the path by hand:

```bash
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --handoff
```

Use the printed path as the write target below. That directory may not exist yet;
creating it via the Write tool is fine.

## Read any existing handoff first

If a `HANDOFF.md` already exists at that path, **read it before writing**. The user may
be iterating, doing several rounds of work before compacting. Treat the existing file as
prior context and **update it in place** (carry forward still-relevant goals and notes,
revise progress, add new dead ends) rather than overwriting it blank. Only drop content
that is genuinely resolved or obsolete.

## Compose the handoff

Write the document from the current conversation. Be concrete and specific. Prefer real
file paths, command names, and decisions over vague summaries. The point is that a fresh
agent with no memory of this session can resume immediately.

Use this structure. Omit a section only when it would be empty (keep **Open questions**
out entirely if there are none):

```markdown
# Handoff

## Goal
What we're trying to accomplish.

## Current progress
What's been done so far.

## What worked
Approaches that succeeded.

## What didn't work
Dead ends, so they are not repeated.

## Key files
Paths and locations touched, so the next session orients fast.

## Next steps
Concrete, ordered action items.

## Open questions
Decisions still pending. Omit this section if there are none.
```

## Write and confirm

Write the composed document to the resolved path with the Write tool, then report in one
line: the path written, and that the next `/compact`, `/clear`, or new session will
inject it and then remove it. Do not run `/compact` yourself; leave that to the user.
