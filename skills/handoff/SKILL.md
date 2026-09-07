---
name: handoff
description: Write a consume-once handoff of the current session's working state into this project's vault, so the next session picks up where this one left off. Most useful right before /compact or /clear. The next fresh session (compact, clear, or startup) injects the handoff and then deletes it.
disable-model-invocation: true
---

# Vault Handoff

Write where the work stands right now to a `HANDOFF.md` in this project's
machine-local state. The file is a transient baton, not durable memory, and it
never reaches the vault. The SessionStart hook injects it into the next fresh
session and then deletes it. The end-of-session sweep records durable learnings
separately. A handoff carries the in-flight task across a `/compact`, a `/clear`,
or a new session.

## Step 1: locate the handoff file

The path resolver derives the project's state directory. Never derive the path by
hand:

```bash
"${CLAUDE_SKILL_DIR}/../../hooks/vault-path.py" --handoff
```

The printed path is the write target. The directory can be missing. The Write
tool creates it.

## Step 2: read any existing handoff

If a `HANDOFF.md` exists at that path, read it before you write. The user can be
iterating, with several rounds of work before a compact. Treat the existing file
as prior context and update it in place. Carry forward the goals and notes that
still apply, revise the progress, and add the new dead ends. Drop only content
that is resolved or obsolete. Never overwrite the file blank.

## Step 3: compose the handoff

Write the document from the current conversation. Be concrete. Prefer real file
paths, command names, and decisions over vague summaries. A fresh agent with no
memory of this session must be able to resume at once.

Use this structure. Omit a section only when it is empty. If there are no open
questions, omit that section entirely.

```markdown
# Handoff

## Goal
What we are trying to accomplish.

## Current progress
What is done so far.

## What worked
Approaches that succeeded.

## What did not work
Dead ends, so they are not repeated.

## Key files
Paths and locations touched, so the next session orients fast.

## Next steps
Concrete, ordered action items.

## Open questions
Decisions still pending. Omit this section if there are none.
```

## Step 4: write and confirm

Write the document to the resolved path with the Write tool. Then report in one
line: the path you wrote, and that the next `/compact`, `/clear`, or new session
injects the file and then deletes it. Do not run `/compact` yourself. Leave that
to the user.
