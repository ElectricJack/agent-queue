OpenCode's `question` tool opens a dialog that blocks this whole session until it closes, and
nobody watches this terminal. AQ turns each such dialog into a durable question for the project
supervisor and keeps your claim while it waits, but every one stalls the task. So:

- Do not ask optional questions: no scope questionnaires, no "which approach do you prefer", no
  "should I continue/proceed" confirmations. The task description, its spec and its comments are
  your instructions. Where they leave a choice open, take the reasonable, reversible option, record
  it with `aq task comment <task-id> --body "Decision: … because …"`, and keep working.
- After an automatic compaction OpenCode tells you to "continue if you have next steps, or stop and
  ask for clarification". Under AQ that means continue: re-read the task with `aq task show
  <task-id>` and carry on.
- Ask only for a decision the task cannot settle, and finish everything that does not depend on it
  first; then ask once, listing the options with your recommended one first. A human-only decision
  (credentials, access, destructive, external or delivery actions, a change of scope) stays blocked
  until a human answers: never work around it.
- If AQ closes your dialog for you, the tool result says the question was dismissed and an
  `[aq question answered]` line follows with a pointer to the answer. Read it and continue.
