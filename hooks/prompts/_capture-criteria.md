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

   Name the work that will hit it next: an open backlog item, a workflow that
   recurs in this repository, or a tool every session runs. "A future feature in
   the same area" or "someone might build X" does not count. If you cannot name
   it, skip it.
2. **Non-recoverability.** Is it absent from the code, types, tests, config, and
   docs, so a future agent would re-make a mistake, re-spend effort, or guess
   wrong about how the user wants things done? Say in one sentence what that
   agent would do wrong without the page. If you cannot, or a quick read of the
   project would stop them anyway, skip it.

   **This gate is answered by reading the repository, never by recalling the
   transcript.** The transcript shows a fact being discovered, and anything
   discovered looks new. Before writing, open the file the fact is about, or the
   diff of the commits this session made, and look for the fact there. Then
   look in this vault's `specs/` and `logs/`: a fact a spec or an earlier log
   states is already recorded. A config value, a code comment, a docstring, a
   test name, or a CLAUDE.md line that states it is proof the gate fails. The
   session's own commits are where a candidate most often already lives, since
   the session that learned a fact usually wrote it down in the code as it went.

   **A candidate that passes this gate must still prove it is worth a page.**
   Its value is how likely it is to come up again times what it costs when it
   does. This is a judgment about the candidate alone, so it needs no search
   beyond the one above. Write a page only when one of these holds:

   - It is likely and costly. It comes up in normal work on this repository, and
     hitting it unwarned costs real time or causes real damage.
   - It is rare but severe. It seldom comes up, but when it does the damage is
     large: data loss, a broken release, a silent wrong result.

   A page whose mistake is trivial to remediate fails, however often it comes
   up: an error message that names its own fix, a one-line retry, or a mistake a
   test or linter catches at once. A preference or coding standard passes only
   when no tool enforces it and missing it costs the user a correction each
   time. To pass, name the concrete mistake a future agent would make without
   the page and what it would cost. If you cannot name both, skip it.
3. **Placement.** Ask this only of a candidate that already passed gates 1
   and 2 and the value test. A candidate that failed any of them is dropped,
   never moved to `backlog.md` instead.

   A page is the default home. A fact belongs in the project's committed
   `CLAUDE.md` only when it changes what EVERY session does, whatever area it
   works in: how the repository is laid out, built, run, tested, or released.
   A fact that matters only while working in one area is a page, even when
   `CLAUDE.md` would be easier to find. Before routing a fact there, read the
   repository's `CLAUDE.md`; if it already says the fact, or something close
   enough that an agent would follow it, drop the candidate.

   You cannot write to the repository, so record a `CLAUDE.md` fact as one
   `docs` item in `backlog.md` naming the line to add, and write no page. Add
   at most one such item per sweep; when several qualify, keep the one whose
   absence costs the most. A fact that is true only beside one function is
   neither a page nor a backlog item: drop it, since whoever changes that code
   reads the code.

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

A page whose core sentence is "the user chose, rejected, removed, or approved X"
is a decision, whatever word frames it. It lives in the spec, the commit, or the
log. A preference counts only when the user stated it as a rule for future work
("always", "never", "from now on"), not as a call about one feature.

A negative finding is not a learning either. "X does not apply here" or "the
forum was wrong about X" earns a page only when this session lost real time to
X, or the code depends on X not applying.

A session that built one feature usually yields no learning: the spec, the
commits, and the log hold it. Write one only for a trap that cost this session
real time and will cost an unrelated session the same.

A page is one fact. Write that fact as one sentence with no "and", "but", or
"so" joining two claims. If it needs one, it is more than one fact or a story:
split it, or drop it. A page that needs a date, a device, "checked on", or "the
owner" in its body to make sense is a log entry, not a learning.

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
- Having failed gate 2, it never reaches gate 3, so it is not a `CLAUDE.md`
  backlog item either.

The right output for that session is no page and no backlog item.

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
  in `backlog.md`. A `CLAUDE.md` line is deferred work only through gate 3.

If a candidate is only true about the specific lines you changed, it belongs in
neither. Drop it.
