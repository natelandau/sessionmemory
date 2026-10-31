## v0.18.0 (2026-10-31)

### Feat

- **local**: support registered folders outside git

## v0.17.0 (2026-10-29)

### Feat

- **local**: stop offering local entries the person declined (#20)

### Refactor

- **cli**: move command logic into lib and speed up startup (#21)

## v0.16.0 (2026-10-26)

### Feat

- **local**: move adopting into its own local adopt command
- **cli**: offer to initialize and register instead of naming a command
- **local**: ask about pending items and conflicts during sync

## v0.15.0 (2026-10-26)

### Feat

- **cli**: regroup the command surface and fill its gaps
- **local**: adopt any file or folder into the vault (#19)
- **local**: ask one plain question per item in local sync

### Refactor

- **cli**: remove the -v/--verbose flag

## v0.14.0 (2026-10-26)

### Feat

- **local**: add local sync and keep settings in the vault by copy (#18)
- reconcile log commits and add a value test for learnings (#17)
- **plugin**: link skills and rules at session start and guard local/
- **local**: keep private skills and rules in the vault

## v0.13.0 (2026-10-25)

### Feat

- **sweep**: add a /sessionmemory:sweep skill (#16)

## v0.12.0 (2026-10-24)

### Feat

- **local**: keep CLAUDE.local.md in the vault and link it

## v0.11.0 (2026-10-13)

### Feat

- **sweep**: lower the default user character floor to 200
- **sweep**: count work-starting slash commands as user messages (#14)

## v0.10.0 (2026-10-13)

### Feat

- **specs**: search specs by meaning and track their status (#12)
- **cli**: add path lookup, decide items, and vault hygiene checks (#11)
- **sweep**: queue concurrent sweeps and validate their writes (#10)
- **runbooks**: name runbooks for their title alone

### Fix

- **sweep**: harden the sweep, key redaction, and registry writes (#13)

## v0.9.0 (2026-10-09)

### Feat

- **sweep**: accept a dedicated Anthropic API key

## v0.8.0 (2026-10-07)

### Feat

- **vault**: add a reference folder for private project docs (#9)

## v0.7.0 (2026-10-07)

### Feat

- **vault**: give runbooks their own folder (#8)

### Fix

- **hooks**: keep SessionEnd inside Claude Code's hook budget

## v0.6.1 (2026-10-02)

### Fix

- **search**: stop returning hits for queries the vault cannot answer

## v0.6.0 (2026-09-14)

### Feat

- **plugin**: log every hook decision to one shared hooks.log

### Fix

- **sweep**: forbid the sweep from creating a MEMORY.md index
- **sweep**: verify learnings against the repo and drop decision recaps

## v0.5.0 (2026-09-07)

### Feat

- **reindex**: add --all to rebuild every project's indexes at once

### Fix

- **skills**: rewrite the four skills as numbered steps in plain prose
- **inject**: count specs instead of listing them and drop plans

## v0.4.1 (2026-09-05)

### Fix

- remove chore prefix from vault checkpoints
- **plugin**: tell a session when no vault is reachable (#5)

## v0.4.0 (2026-09-04)

### Feat

- **new**: add a backlog item with sessionmemory new backlog

### Fix

- **backlog**: delete a finished backlog item instead of ticking it
- **new**: date a spec, plan, or backlog item by the local calendar

## v0.3.1 (2026-09-03)

### Fix

- **specs**: name a spec or plan for the day it was created

## v0.3.0 (2026-09-03)

### Feat

- **log**: record the transcript path and online link on a session log
- **plugin**: split review into a learnings pass and a backlog pass
- **plugin**: judge learning titles by subject, condition, and history
- **cli**: teach a session the vault layout and the backlog format at start
- **plugin**: register a git repository at session start (#4)
- **plugin**: run the sessionmemory on PATH when it passes a version handshake
- **cli**: read the vault root from sessionmemory.toml when unset

### Fix

- **log**: name a session log for the minute its session began
- **plugin**: stop the sweep recording its own session's change as a learning
- **plugin**: name a missing working directory in the sweep log
- **cli**: create a project's folder when it is registered
- **cli**: sort injected titles by their first word, not their first character

## v0.2.0 (2026-09-03)

### Feat

- initial commit
