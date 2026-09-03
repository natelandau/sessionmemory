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
