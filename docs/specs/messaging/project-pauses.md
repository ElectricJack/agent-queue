# Project pause and human notifications

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
  Withdrawn or approved requests must not be announced later.
- The dashboard labels open escalations and human reviews **held while paused**.
  This is a projection of current project status, not an approval or a new
  incident/review state. Existing dashboard decisions remain available.
- Pausing sends a durable notice to each live worker holding a task in that
  project, including workers holding durable waits. Workers stop at the next
  safe point, preserve their progress and claim, and wait for resume before
  submitting reviews or human asks. Resume sends a corresponding notice.
  Prime documents repeat the pause instruction, including with local overrides.
- Resume releases pending items through their existing delivery pumps. The
  hold survives a daemon restart because project status and outboxes are durable.

Focused checks cover silence, resume, withdrawal during pause, retry and lease
preservation, project isolation, mid-attempt pauses, worker notices and dashboard
labels. No operator database or real chat transport is used in these checks.
