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

Migration `a00000000062` adds the three context tables idempotently, including
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
