# Durable playbook state

The D1 storage foundation provides namespace state and retained operation receipts.
The `state_v1` feature gate remains unsupported until D2 supplies transactional
notifications and preview behavior; these commands do not by themselves enable a
stateful artifact for activation.

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
and advances a replay floor that D2 must check before accepting observations.

List snapshots use the shared observation snapshot service: a stable database
view, bounded manifests, at most 500 entries and 192 KiB per page, a 262,144-byte
response ceiling, and a 30-minute lifetime. Continuations bind the namespace,
filters and current authority. Exhausted snapshot budgets fail visibly rather
than truncate a successful scan.

D2 can join `locked_namespace`, `operation_row`, `entry_row(lock=True)`,
`write_entry`, `commit_operation` and private-value batch persistence on the same
SQL connection as message insertion. `check_observation_floor` rejects retired
evidence. D2 owns notification transitions, recipient authorization and atomic
message creation; D1 does not enqueue messages.
