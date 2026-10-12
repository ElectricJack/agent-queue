# Project supervisors are opt-in

Project `supervisor_enabled` is a persisted, non-null boolean, defaulting to
false for new and existing projects. The operator can edit it through
`edit_project`, `aq project edit`, and both dashboard project settings views.

For a disabled project, logical `supervisor-<pid>`, runtime
`n-supervisor--<pid>`, and project-scoped `profile:supervisor` messages route to
`session:supervisor-global`. Preserve the message's project, thread, sender,
priority and identity. The address suffix selects the recipient project;
the sender's project context can differ. The global address remains global.
Internal event notices and existing unread backlog obey the same routing.
Read or archived history is unchanged.

The session lens refuses direct cold starts and wakes of disabled project
supervisors. Disabling records stop intent; reconciliation waits for the
current turn to finish, then stops the instance. This also drains project
supervisors already running when the default changes. Enabling is lazy: the
next message starts the project supervisor. Doctor reports live supervisors
of disabled projects, including those still draining.

Acceptance checks cover schema/migration defaults, all three recipient forms,
project context preservation, internal notices/backlog, cold-start/wake
refusal, enable/start and disable/drain/stop, settings persistence and doctor.
