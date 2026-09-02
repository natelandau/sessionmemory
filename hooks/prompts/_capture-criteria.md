## Who you are writing for

A FUTURE agent working in THIS repository who is NOT working on whatever this
session worked on. They already have the code, git history, tests, types,
config, and docs. Memory is ONLY for what those won't tell them. Most sessions,
especially small, targeted fixes, should add little or nothing. When in doubt,
leave it out: clutter is expensive to remove.

## The two-gate test, a candidate earns a place only if BOTH are yes

1. **Generality.** Would this help a session working on a DIFFERENT part of this
   repository? If it only matters while touching the exact code you touched
   today, the code + commit + tests already hold it. Skip it.
2. **Non-recoverability.** Is it absent from the code, types, tests, config, and
   docs, so a future agent would re-make a mistake, re-spend effort, or guess
   wrong about how the user wants things done? If a quick read of the project
   would reveal it, skip it.

A bug you fixed is NOT automatically a learning: the test you added encodes it.
A learning survives only if it's something the test/code does NOT make visible.

## What is worth capturing

- **Traps & constraints**, non-obvious footguns, invariants, tooling/environment
  gotchas a future agent would naturally violate.
- **Preferences & standards**, how the user wants things done here: coding
  standards, conventions, library/tool choices, workflow preferences they stated
  or clearly demonstrated.
- **Design intent**, why the project is shaped the way it is, when it isn't
  obvious from the code.

Knowledge about a tool or a library that would be true anywhere is still
recorded here when a session in THIS repository would need it again. There is no
shared scope; every project keeps its own copy of what it needs.

## Where each kind goes

- A **learning**: a trap, constraint, preference, or design intent a future
  session in this repository would otherwise get wrong. One page per fact.
- **Deferred work**: something concrete that was decided but not done. One line
  in `backlog.md`.

If a candidate is only true about the specific lines you changed, it belongs in
neither. Drop it.
