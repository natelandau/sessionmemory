---
name: sweep
description: Record this session into the project's vault now, in this session, instead of leaving it to the automatic sweep that runs a separate model when the session ends. Writes learnings, backlog items, spec status, and the session log, then runs the same safety checks the automatic sweep runs. The automatic sweep then covers only work done after this one.
disable-model-invocation: true
---

# Sweep This Session

Do the end-of-session memory sweep yourself, here, with the conversation you
already have. The automatic sweep reads a copy of this transcript in a separate
model run. Once this sweep finishes, the automatic sweep at session end or before
a compaction covers only what happens after it.

## Step 1: get the sweep instructions

Run the prompt script. It prints the instructions the automatic sweep follows,
filled in for this project and this session, and records the project folder
before you write anything:

```bash
"${CLAUDE_SKILL_DIR}/../../hooks/sweep-prompt.py"
```

When it exits non-zero, report its message to the user and stop. Nothing was
started, and the automatic sweep still covers the session.

## Step 2: follow them

Do what the printed instructions say, for this conversation. They decide whether
the session is worth recording, and if it is, they cover learnings, deferred
work, spec status, and the session log. Run every command exactly as printed,
the log command above all, since it carries this session's id and title.

When the instructions say to record nothing and stop, write nothing, but still
do step 3.

## Step 3: check what you wrote

Always run the check script last, whether or not you wrote anything:

```bash
"${CLAUDE_SKILL_DIR}/../../hooks/sweep-check.py"
```

It runs the automatic sweep's safety checks on every file you changed in the
project folder: it redacts secrets, sets aside a page with the wrong shape, and
puts back `CLAUDE.local.md`. Its success is also what tells the automatic sweep
this one finished, so never skip it and never run it in a later turn.

## Step 4: report

Tell the user in a few lines what you recorded: each learning's title, each
backlog line, any spec whose status you changed, and the log's summary. Then
pass on every note the check printed. When you recorded nothing, say so in one
line. Do not commit the vault; the hooks do.
