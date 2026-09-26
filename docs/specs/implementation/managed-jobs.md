# Managed jobs: phase 2 execution substrate

Implements plan (2) of the approved 2026-09-24 exclusive job queue and managed
long-running command specs in the agent-queue project vault. Phases 2 and 3 leave
`resources.jobs.enabled: false`. Phase 3 exposes scoped CLI/MCP/typed API
commands, atomic job waits and existing wait-result delivery; publisher and finite
stream adapters belong to phase 4. The internal reconciler remains excluded from public transports. There is one executor and one
identity: `jobs.id`, `AQ_JOB_ID`, and `<data_dir>/runs/<uuid>/`.

`src/jobs/service.py` orders accepted jobs by aged band, timestamp and id. An
exclusive frontier drains existing shared work. The detached `src.jobs.runner`
supervisor uses the existing versioned box locks, inherited by its command's
children, and never accepts interpolated shell text. Finite test/lint/build/e2e
presets construct executable argv on the server. A configured disposable
`resources.jobs.test_database_url` is required for test/e2e presets; the worker
DB refusal guards remain present and no daemon/session/provider credentials
are inherited. This is separation on one OS account, not a sandbox.

Submission snapshots its validated contract, environment caps and deadlines.
Reusing the same owner/idempotency key and request returns the original row;
changing the request is refused. Quotas and output reservations serialize in
PostgreSQL. Terminal transitions use state-version CAS, persist immutable result
v1 and append `job:<id>:terminal` to `job_outbox` in the same transaction. The
job adapter scans durable terminal state, and the existing wait outbox sends
one result pointer through the message engine.

Workspace pins have no expiry. Their workspace-row count fences acquisitions,
releases and deletion; a transaction advisory lock serializes pin creation with
checkout/reset/cleanup mutations. Pins survive a session stop. A completion
receipt plus a verified absence of marked processes releases a pin. Missing or
unverifiable launch identity leaves a visible `cleanup_blocked` pin; elapsed time
never clears it or launches a replacement. `aq doctor --check
resources.jobs_cleanup --fix` retries reconciliation without guessing a PID.

The runner takes an instance lock, writes fsync-renamed launch intent and started
receipts, executes argv with null stdin, pumps stdout/stderr and enforces its
own run deadline. It reaps marked descendants before writing `completion.json`.
Adoption checks boot id, process start ticks and nonce. Daemon loss does not own
execution lifetime, and disabling new admission still reconciles existing jobs.

`output.head` keeps the first 1 MiB and `output.tail` is a bounded 63 MiB byte
ring. The fsync-renamed manifest records logical offsets and physical-block
hashes. Interrupted overwrites become explicit gaps; they cannot return fresh
bytes under old offsets. Giant lines cannot exceed the cap. The shared streaming
pytest/JUnit parser builds result v1 with an 8,192-byte failures-first excerpt;
exit 5 fails, a missing receipt is lost, and output-store failures cannot pass.
Result hashes refer to the canonical stored artifact; a response may shorten
its rendered excerpt. Live-tree fingerprints are advisory and explicitly
unverified, never integration attestations.

Bounded reconciliation sweeps retain terminal logs for 14 days and results for
90 days. Output reservations use a 2 GiB default host budget and evict terminal
logs before refusing admission. Active output is never evicted. Task deletion
revokes result/log access; owner termination cancels execution before cleanup.


Phase 3 registers `job_submit|get|list|cancel|result|logs` contracts and grants in
worker templates. `aq job submit --wait` and `aq test --aq-detach --aq-wait`
commit the job, pin, output reservation and wait together. Replays retain one
job/pin/wait, including after terminal completion. Job waits snapshot already
terminal producers and arbitrate using `ended_at`, so a late scan preserves
completion at or before the deadline. Default deadlines follow the remaining
queue/run budgets plus 300 seconds, capped at 24 hours. Explicit timeout policy
is retained in the typed match for stable replay checks. Expiry and wait
cancellation never implicitly cancel execution or release pins.

Submission locks session/task before the global quota and workspace locks;
ordinary submissions use the same task-before-quota order. A refused wait rolls
back all new producer state. A worker can observe only jobs belonging to its
task; supervisors can subscribe within their project. Terminal digests retain
actual failure/cancellation/lost outcomes and point to `aq job result ID`.
Installed worker templates need the usual grants reseed; see the
[wait guide](../../guides/agent-waits.md). No schema revision is needed in phase 3.

### Wait reconciliation and pool delivery

The session cascade and targeted lease reconciliation use the orchestrator's
installed command handler (`_command_handler`). Both paths resolve durable
conditions through `reconcile_agent_waits`; a missing or rejected handler must
not silently leave a completed or overdue wait active. Task-result messages
resolve the session currently attached to the owner task, including pool
sessions whose names are independent of the task id. An idle current holder
receives the result pointer once; busy or absent holders retain queued results.
Delivery never starts a task worker or changes the task's status.

`aq doctor --check waits.pending_terminal_tasks` reports active task-kind waits
whose same-project producer is COMPLETED, FAILED or BLOCKED, including archived
producers. It reports wait, owner and session ids and the target status in a
bounded diagnostic. This is read-only: the normal command reconciler owns
resolution and the result outbox.
