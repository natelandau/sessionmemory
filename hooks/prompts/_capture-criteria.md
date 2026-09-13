## Who you are writing for

A FUTURE agent working in THIS repository who is NOT working on whatever this
session worked on. They already have the code, git history, tests, types,
config, and docs. Memory is ONLY for what those won't tell them. Most sessions,
especially small, targeted fixes, should add little or nothing. When in doubt,
leave it out: clutter is expensive to remove.

## The three-gate test, a candidate earns a page only if ALL THREE are yes

1. **Generality.** Would this help a session working on a DIFFERENT part of this
   repository? If it only matters while touching the exact code you touched
   today, the code + commit + tests already hold it. Skip it.
2. **Non-recoverability.** Is it absent from the code, types, tests, config, and
   docs, so a future agent would re-make a mistake, re-spend effort, or guess
   wrong about how the user wants things done? Say in one sentence what that
   agent would do wrong without the page. If you cannot, or a quick read of the
   project would stop them anyway, skip it.

   **This gate is answered by reading the repository, never by recalling the
   transcript.** The transcript shows a fact being discovered, and anything
   discovered looks new. Before writing, open the file the fact is about, or the
   diff of the commits this session made, and look for the fact there. A config
   value, a code comment, a docstring, a test name, a CLAUDE.md line, or a
   commit message that states it is proof the gate fails. The session's own
   commits are where a candidate most often already lives, since the session
   that learned a fact usually wrote it down in the code as it went.
3. **Placement.** Would a comment beside the code, or a line in the project's
   committed `CLAUDE.md`, serve the next agent better than a page they have to
   search for? A fact that every session needs, about how the repository is
   laid out, built, run, or released, belongs in `CLAUDE.md`, where every
   session reads it without searching. A fact that is only true beside one
   function belongs in a comment there. You cannot write to the repository, so
   record such a fact as one `docs` line in `backlog.md` naming what to write
   and where, and write no page.

The work this session did is not a learning. A bug you fixed is encoded by its
test. A change you made, and why, is encoded by the commit message and the
comment beside the code, and a page that restates either is a copy of the log.
A learning is something that would still be true if the code this session
touched were deleted. This session's commit may be where a trap was found,
never what the page is about.

A decision this session made is not a learning either, and neither are the
alternatives it rejected or the reasons for rejecting them. A rejected
alternative is not in the code because it was rejected; that absence is not
non-recoverability. The decision lives in the commit that made it, and a page
that says "X was considered and rejected because Y" is the commit message
written a second time.

## A page that fails every gate

A session sets up commitizen at the root of a repository holding an API under
`api/` and a web client under `web/`, and along the way the user declines to
move the Python project to the root. The sweep is tempted to write "Commitizen
config is at repo root; Python project stays in api/", listing the version
files it bumps, the pre-bump hook, the `just bump` recipe, and why the move was
rejected. Every part of that fails:

- The config file, the hook, and the recipe are the first thing an agent
  running a release opens, and the config's own header comment says why it is
  at the root. Gate 2 fails on a read of the repository.
- The rejected move is a decision this session made. The commit holds it.
- "Where the release tooling lives" is something every session needs, so if any
  of it were missing from the repository it would be a `CLAUDE.md` line, not a
  page. Gate 3 fails.

The right output for that session is no page.

## What is worth capturing

- **Traps & constraints**, non-obvious footguns, invariants, tooling/environment
  gotchas a future agent would naturally violate.
- **Preferences & standards**, how the user wants things done here: coding
  standards, conventions, library/tool choices, workflow preferences they stated
  or clearly demonstrated.
- **Design intent**, an invariant the code depends on but cannot state, which a
  future agent would break without knowing it. Not the history of how the code
  came to be shaped this way, and not the shapes it was not given.

Knowledge about a tool or a library that would be true anywhere is still
recorded here when a session in THIS repository would need it again. There is no
shared scope; every project keeps its own copy of what it needs.

## Where each kind goes

- A **learning**: a trap, constraint, preference, or design intent a future
  session in this repository would otherwise get wrong. One page per fact.
- **Deferred work**: something concrete that was decided but not done. One line
  in `backlog.md`. A fact that belongs in `CLAUDE.md` or a code comment is
  deferred work of kind `docs`.

If a candidate is only true about the specific lines you changed, it belongs in
neither. Drop it.
