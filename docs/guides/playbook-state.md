# Durable playbook state

The `state_v1` feature provides namespace state, retained operation receipts and
transactional notifications. Stateful artifacts can be activated when all intended
executors support this feature.

Ordinary state commands require a live playbook invocation and its current command
grant. The daemon derives the namespace from the pinned artifact and verified
source: scope kind, scope owner, playbook ID and `state_schema_version`. System
scope uses owner `system`; project and profile scopes require matching authority.
Command arguments cannot select another namespace. Operator inspection and reset
require independent local or service authority.

| Command | Inputs and behavior |
|---|---|
| `playbook_state_get` | `key`, optional `record_space` (`kv` or `notification`). Returns existence, value, revision, tombstone and expiry metadata. |
| `playbook_state_list` | Optional prefix, record space and snapshot paging inputs. Returns immutable pre-scan revisions and protected pages. |
| `playbook_state_set` | KV key, JSON value, `expected_revision`, `operation_key`, optional positive integer `ttl_seconds`. |
| `playbook_state_delete` | KV key, expected revision and operation key. Creates a retained tombstone. |
| `playbook_state_notify` | Key, `present` or `cleared` action, `observed_at`, stable `observation_id`, and a bounded message. Optional evidence, source partition and reminder interval; clears require `expected_revision`. |
| `playbook_state_inspect` | Explicit `namespace` object plus key or list filters. Operator only. |
| `playbook_state_reset` | Namespace, expected **namespace** revision, operation key and nonblank reason. Operator only. |

Revision zero means the typed key has never existed. Expiry makes a value logically
absent but retains its revision. Deletion and reset increment revisions; neither
allows an old revision-zero writer to recreate a key. An identical operation key
and request returns the original protected receipt before CAS validation. Reusing
the operation key with different inputs returns `idempotency_conflict`; stale CAS
returns `conflict`. Authorization is checked again even for replay.

State values and journal results use the private-value encryption service. Missing
encryption keys refuse writes and protected readback. Logs and command events
redact state inputs and results, including operator calls. State set/delete only
write KV entries; notification metadata remains separately queryable and has no
automatic TTL.

Each namespace permits 10,000 typed keys, 256 KiB per value, 16 MiB of logical
current value payload and 256 MiB of retained journal accounting. Tombstones count
as keys; expired values remain accounted until explicitly replaced or reset.
Journal accounting includes canonical request, result and operation-key bytes.
Quota exhaustion refuses the whole transaction. A PostgreSQL namespace row lock
serializes missing-key creation, journal replay and quota checks before entry
locks. There is no automatic tombstone, journal or historical protected-value GC.

Version 1 is provisioned on first write. A source version change emits a proposal
warning because it selects a different namespace and resets suppression. A later
version must first be explicitly provisioned by an operator after review (reset
with expected namespace revision zero). Reset records its reason and the warning
that future observations may deliberately re-alert. Existing namespace state and
journals survive recompilation, activation changes, disable/re-enable and source
removal. Reset retains notification episodes, watermarks and message identities,
and advances a replay floor checked before accepting new observations.

List snapshots use the shared observation snapshot service: a stable database
view, bounded manifests, at most 500 entries and 192 KiB per page, a 262,144-byte
response ceiling, and a 30-minute lifetime. Continuations bind the namespace,
filters and current authority. Exhausted snapshot budgets fail visibly rather
than truncate a successful scan.

Notifications require both `playbook_state_notify` and `message_send` in the current
principal and pinned artifact grants. A notify step's artifact grants `message_send`
when its source declares `required_capabilities: [..., message_send]`; no extra
`message_send` step is needed, and an undeclared dependency stays denied. The message object accepts only `to_kind`,
`to_id`, `body` (up to 16,384 characters) and optional `subject` (512 characters).
Sender identity is server-derived: `system:playbook:<id>`. Message readback includes
run, playbook, namespace and step provenance. Project playbooks can address only
authorized project recipients. Shared submission validation checks recipients on
every call, including suppression and replay.

A first `present` observation starts an episode and enqueues a message. Later
observations update evidence and advance the revision without sending, unless a
positive `repeat_after_seconds` interval has elapsed since the last enqueue.
Omitting the interval or setting it to null disables reminders. Reminders use
database transaction time; observation timestamps order evidence. A `cleared`
observation enqueues once for an active episode and retains an inactive tombstone.
A later presence starts another episode. Clears require the revision saved before
the observation scan, including for missing or already inactive keys.

The notification operation identity combines namespace, key, observation ID and
action. Identical retries return the original result; changed inputs under that
identity conflict. Older observations are refused, and different observations at
an equal timestamp conflict. Missing-key clears retain their watermark so delayed
presence cannot reopen an older episode. `last_value` uses protected storage;
`source_partition` is bounded identity metadata used to match complete scans.

Message insertion, state, protected values, journal and quota accounting commit in
one PostgreSQL transaction. Failure rolls all of them back. After commit,
`message.sent` wakes the existing delivery service; its backlog scan recovers after
a lost wake or restart. The guarantee is one durable enqueue per accepted operation,
not exactly-once terminal display or human attention.

Dry-run and shadow use fresh in-memory state for each run and copy it at symbolic
branches. They share the live notification transition logic and simulate KV reads,
CAS, expiry and immutable snapshots. They never read or write production state or
protected values. Logical supervisor and system user recipients can be simulated;
recipients requiring a database ownership lookup remain explicitly unresolved.
Preview message and snapshot IDs are simulation-only and are not durable handles.
