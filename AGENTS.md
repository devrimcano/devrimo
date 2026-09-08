# Codex task guidance

## Model and delegation

- Prefer `gpt-5.6-luna` with `max` reasoning whenever those controls are available.
- Delegate aggressively when work can be split into independent investigation,
  implementation, testing, documentation, or review tasks.
- Run parallel subagents when that can save time or improve coverage; do not
  delegate trivial work or tightly dependent edits merely to increase the
  number of agents.
- Keep the main agent responsible for task scope, integration, conflict
  resolution, security-sensitive decisions, and final verification.
- Give each parallel subagent a clear ownership boundary. Do not let parallel
  agents edit the same files unless the main agent explicitly coordinates it.
- Require each subagent to report the files it inspected or changed, commands
  it ran, and any unresolved risks or failures.

## Completion

- Run the tests and checks appropriate to the change before considering the
  task complete.
- Integrate subagent results, review the complete diff, and verify the final
  behavior from the user's perspective.
