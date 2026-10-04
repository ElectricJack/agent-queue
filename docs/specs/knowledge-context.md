# Knowledge context delivery (K08)

Implementation of section 9 of the approved 2026-10-01 work and knowledge
records plan. Core knowledge is contextual evidence. Required session
instructions, approvals and task state retain their own authority and order.

`ContextService` prepares one ordered `ContextBundle` for worker prime,
named supervisor bootstrap and retained orchestrator prompt assembly.
`PrimeRenderer` and `PromptBuilder` render that prepared selection without
retrieving or writing citations. JSON and Markdown carry the same exact
record/revision/hash, evidence labels and retained source descriptors.

Preparation requires a live session instance and an existing open task
attempt with the current claim epoch. Pool tokens resolve their current task
from the live session; no attempt ID is manufactured. A supervisor's
prelaunch identity is daemon-created and narrowed to the intended profile,
session instance and explicit scopes. Acknowledgement requires the persisted
live session. Global supervisors default to no private corpus; an operator
can select at most eight project IDs or explicitly enable global summaries.

Pins are exact revisions and precede lexical discovery. No query produces no
discovery. Discovery excludes stale, future, retired and disputed evidence;
explicit pins retain their labels. Each read uses core scope, source, revision,
sharing and authority checks. A valid repeated preparation reuses its bundle;
`refresh=True` creates a new one. Delivery rechecks ownership, capabilities,
expiry, configured budgets, exact payloads, current discovery heads and live
sharing/authority. Redaction erases cached selection bodies in the same
transaction, preserving identity-only citations and receipts.

The conservative `upper_bound_bytes` method counts UTF-8 bytes as an input
token upper bound, including required instructions, tools, separators and
knowledge. It also reserves wrapper and output allowances. The defaults are
32768 total tokens, 4096 output reserve, 256 wrapper reserve, 4096 knowledge
tokens/16384 bytes and eight discovered items/1000 discovery tokens. Knowledge
uses the minimum of its hard cap, `memory.context_max_tokens` and remaining
input allowance. Whole items are dropped with omission reasons. Required
content overflow stores an empty diagnostic bundle and refuses delivery with
`context.required_over_budget`; authoritative instructions are never cut.

`prime` returns `context_bundle` and `context_state: prepared`. The K09
CLI/hook adapter calls `knowledge_context_deliver` only after successful
transport output, with bundle ID, rendered knowledge SHA-256, transport,
idempotency key and current claim epoch. Preparation and `failed`/`unknown`
receipts create no injected citations. A successful supervisor start followed
by session persistence acknowledges delivery. Duplicate acknowledgements do
not duplicate usage; `delivered` never regresses. A failed acknowledgement
leaves the session running and is logged as a context diagnostic.

`knowledge_cite` separately accepts `attached` or `explicit_read`, an exact
revision and an idempotency key. Search and ordinary show do not imply usage.
These citations work independently of context/memory injection flags. Neither
delivery nor a citation proves model comprehension, obedience or approval.

Migration `a00000000063` adds the three context tables idempotently, including
exact record/revision foreign keys and exclusive execution-owner constraints.
Execution references remain soft for archival. Downgrade refuses any retained
bundle, receipt or citation; populated installations use read-only rollback.

`knowledge.enabled`, `knowledge.context.enabled` and `memory.enabled` must all
be enabled, with explicit pilot project scopes. Defaults remain off. Activation
is the operator's G4 decision after K09 and its evaluation evidence; this
increment does not enable production injection, migrate the operator database,
import legacy memory or call a paid provider. Shipped worker/supervisor grants
include the citation and delivery commands. Existing worker profiles remain
write-if-absent and require the normal operator grants-only reseed before
activation. When a bundle is supplied, legacy L1/L2 tiers are suppressed.

## Harness delivery and resume adapters (K09)

`src/knowledge/delivery.py` wraps and routes one already-prepared selection. It
never ranks, summarizes, verifies or acquires authority, and it has no database
or daemon dependency, so `aq prime --hook-json` can import it directly. One
payload leaves every transport byte-identical; only the wrapper differs.

Four transports, classified from what a launch actually provisioned — not from
what a harness could do. A hook whose trust was withheld is not a hook:

| Transport | Condition | Wrapping |
|---|---|---|
| `hook_envelope` | `claude`/`codex` with hooks provisioned | `SessionStart` hook JSON |
| `startup_prompt` | any launch with a prompt channel | plain body |
| `startup_guidance` | no hook and no prompt channel | `.aq/knowledge-startup.md` written into the workspace |
| `memory_pointer` | opt-in managed pointer block in a provider memory file | markers around AQ commands and the authority boundary |

Duplicate suppression is transport-only: a startup prompt that already carried
the payload suppresses the hook envelope, while `compact` and `resume` always
deliver — those are exactly when continuation state pays for itself. The
acknowledgment key is derived from transport, bundle, source, session and claim
epoch, so a retried receipt deduplicates and a recycled slot cannot reuse the
previous task's key. `acknowledge()` reports `delivered`, `failed` or `unknown`;
`unknown` is a written payload whose receipt did not complete, and no state
claims comprehension.

`aq prime` acknowledges only after the body is written, and falls back to
recording `unknown` when the receipt does not land. `SessionSpecBuilder` records
the provisioned transport on the spec and writes the guidance file for a launch
that cannot carry the payload in argv. Compaction and resume reprepare under the
current execution identity; a provider or model switch recalculates the budget
while the authorized corpus and pinned evidence stay consistent.

`scripts/evaluate-knowledge.py` is the provider-neutral fixture runner. Its
default adapter replays sealed synthetic observations; `--adapter context-bundle`
and `--adapter local-model` replay the same independent oracle against the real
service in a **disposable** database named with `--db-url`, which is recreated
for the run and dropped afterwards. Worker sentinels, the maintenance database
and the operator's production database are refused. Integrated fixtures under
`tests/fixtures/knowledge/integrated/` seal each record's exact revision hash, so
the oracle compares the adapter's reported hash against sealed bytes instead of
hashing the excerpt it rendered. The local-model adapter models a CLI with
neither hook nor prompt argument: it proves contract compatibility, never
local-model quality.

Activation remains the operator's G4 decision. This increment adds no
migration, enables no feature flag, imports no data and calls no provider.
