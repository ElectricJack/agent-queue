# Project pause and human notifications

This is a current delivery contract. See the [messaging-and-human-asks system
spec](../systems/messaging-and-human-asks.md) for ask ownership and transport modes.

Project status `PAUSED` holds external human requests as well as new task
assignments. Escalations and review revisions may still be recorded by an
already running worker, but their durable outboxes must remain silent until
the project resumes. Human gates use the same escalation delivery boundary.

- Pending escalation deliveries, retries and expired leases belonging to paused
  projects are not claimed and do not consume delivery attempts or block another
  project's notifications or the installation digest.
- Delivery rechecks project status before external writes. A pause during an
  attempt preserves confirmed bindings and returns the remaining work to the
  outbox; an already submitted external request cannot be recalled.
- Review notifications retain their revision cursor while paused. On resume,
  announce the latest pending revision once, only if it still awaits review.
  A request withdrawn or decided while its project is paused is retired from the
  outbox at that transition (`transition_review`), so resume never announces it;
  outside a pause the outbox semantics are unchanged.
- The dashboard labels open escalations and human reviews **held while paused**.
  This is a projection of current project status, not an approval or a new
  incident/review state. Existing dashboard decisions remain available.
- Pausing sends a durable notice to each live worker holding a task in that
  project, including workers holding durable waits. Workers stop at the next
  safe point, preserve their progress and claim, and wait for resume before
  submitting reviews or human asks. Resume sends a corresponding notice.
  Routine notices to a worker in a verified wait remain subject to its held-mail
  fence; writing a pause notice does not itself wake or satisfy that wait.
  Prime documents repeat the pause instruction, including with local overrides.
- Pausing also stops the project's supervisor agent. The session lens refuses
  to cold-start or wake it, and reconciliation drains the running one after its
  current turn, recording `project_paused` as the stop reason. The pause never
  flips the operator's `supervisor_enabled` intent, so resume restarts the
  supervisor only when it was enabled before the pause — a supervisor that was
  off before the pause stays off. The global supervisor is not project-scoped
  and is unaffected by any pause or resume.
- Resume releases pending items through their existing delivery pumps. The
  hold survives a daemon restart because project status and outboxes are durable.

Focused checks cover silence, resume, withdrawal during pause, retry and lease
preservation, project isolation, mid-attempt pauses, worker notices, dashboard
labels, supervisor stop/drain on pause, no respawning while paused,
resume re-arm only for a previously-enabled supervisor, and wake refusal
while paused. No operator database or real chat transport is used in these checks.
