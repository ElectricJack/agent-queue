# System spec: reviews and knowledge

Status: mechanisms and gaps audited against `52d2efe4c` (origin/dev, 2026-10-22).
The original 2026-10-20 inventory, live observations and task-state snapshot below
remain dated evidence, not current deployment or completion claims. This is system 11 of the
eighteen in the agent-queue system inventory (vault
`projects/agent-queue/specs/2026-10-20-agent-queue-system-inventory.md`, review
`rev-bright-rapids`, approved at revision 1). Written for epic eager-vault-55, task
eager-vault-55.11. It explains what the system is for, how it works today, where it stops,
and what is planned. It links to the existing guides and design documents rather than
repeating them. The inventory merged four things into this system: document reviews, spec
ingestion, knowledge records, and the older scoped-memory layer. The inventory's own phrase
for the merge is "reviewed documents in, versioned knowledge out".

Vault documents are cited by their file name under `projects/agent-queue/` in the operator
vault, with the review id and approval state read on 2026-10-20. Those files are not in this
repository.

## 1. Purpose

Agent Queue (AQ) produces two kinds of written output that must not be treated like code.

The first kind is **documents that a human decides on**: specs, plans, design proposals and
research reports. They are not committed to the repository. Instead they are submitted as
numbered revisions that Jack, the operator, approves, sends back, or rejects. An approved
spec or plan then has to become work. Turning a long document into a task graph is itself
error-prone: goals get dropped, scope creeps in, or a whole graph lands before anyone has
checked that it matches the document.

The second kind is **what the agents learn**: facts about the codebase, decisions,
procedures, incident notes and research findings. These must survive beyond one session and
be findable by the next agent, without letting one agent's guess turn into something every
later agent obeys.

This system owns:

- **Document reviews**: revisions, inline comments, attachments, adversarial reviewer
  dispatch, the decision, and the vault mirror of each document.
- **The review gate**: a gate that holds filed work until a document is approved
  (`--after-review`).
- **Spec ingestion**: turning an approved spec or plan into a task graph. For an
  implementation spec, the graph is held until a fidelity reviewer has checked it against
  the document.
- **Knowledge records**: typed, versioned, linked and searchable records, with explicit
  rules for authority, sharing, erasure, import and deprecation.
- **Context delivery**: a bounded bundle of relevant knowledge added to an agent's start-up
  prompt and to `aq prime`, with a ledger of what was actually delivered.
- **Legacy memory**: what is left of the older memory layer (the extracted `aq-memory`
  plugin, consolidation and reflection) and the fences that keep it from writing.

The problems it solves are:

- a human, not an agent, decides on every user-decided document, and every decision is tied
  to the exact revision and body hash that was reviewed;
- feedback is never lost between a decision and the task that answers it;
- work that depends on a document cannot start before the document is approved;
- an approved implementation spec becomes a graph that someone has checked, not a graph
  that is trusted because an agent wrote it;
- knowledge has history, sources and an explicit authority level, so a verified, reviewed
  record is distinguishable from an agent's note;
- agents of every harness see the same knowledge through one bounded context bundle.

## 2. How it works today

### 2.1 Vocabulary

- **Review**: one document under decision. Its id has the form `rev-<adjective>-<noun>`. It
  has a kind (`spec`, `plan` or `other`), a decider and a state. "Design proposal" and
  "research report" are submitted as kind `other`.
- **Revision**: one immutable body of a review, numbered from 1. A revision of a spec
  carries a `spec_kind` of `design` or `implementation` from its frontmatter.
- **Decider**: who may decide the review. `user` means Jack only. `user_or_supervisor`
  means Jack or the project's live supervisor. `supervisor` means the supervisor, for
  internal reviews that never notify Jack.
- **Decision**: one of `approve`, `request_changes`, `fold_in`, `decline` or `reject`.
  Decisions are made through the asks system (§2.3).
- **Feedback obligation**: one comment or note from a decision, with a disposition such as
  `fold_in` or `follow_up` that some task must answer.
- **Revision task**: the task filed to answer requested changes. Its title has the form
  `Revise <title> (review <id>)`.
- **Dispatch**: an adversarial review task sent to an independent reviewer, pinned to one
  revision.
- **Review gate**: the `gates` row of type `review` owned by each review. Tasks filed with
  `--after-review` wait on it.
- **Held batch**: the task graph created from an approved implementation spec. Its tasks
  exist but cannot be claimed until a fidelity review passes and the batch is released.
- **Fidelity reviewer**: the role that checks a held batch against its spec, makes scoped
  edits and records a receipt.
- **Knowledge record**: a versioned record of what is known. Its alias has the form
  `kn-<32 hex>`. It has a category (`fact`, `decision`, `policy`, `procedure`, `incident`,
  `reference` or `note`), a lifecycle and a verification state.
- **Authority**: a property computed when a record is read. A record is authoritative only
  when it is verified, current and active, holds an active grant, and is bound to a
  document review that is still approved.
- **Context bundle**: the bounded set of knowledge items prepared for one session or prime
  call, with a content hash.

### 2.2 Document reviews and their data model

The operator guide, [Reviews](../../guides/reviews.md), explains how to submit, revise,
decide and troubleshoot. The original design is vault
`specs/2026-09-21-document-review-design.md` (status implemented; Jack approved it in chat
on 2026-09-21, before reviews existed, so it has no review id). Its plan,
`plans/2026-09-21-document-review.md` in the vault, is approved but predates the review
system, so it has no review id. This section covers the model.

**State.** The database is the source of truth. The tables are in `src/database/tables.py`:

| Table | Line | Holds |
|---|---|---|
| `doc_reviews` | 839 | One row per review: kind, title, unique `vault_path`, `current_revision`, `state`, `decider`, `gate_id`, `pending_decision_id`, the decision and withdrawal fields, and the Discord cursor `notified_revision` |
| `doc_review_revisions` | 881 | One row per (review, revision): body without frontmatter, `content_sha256`, `spec_kind`, submitter, `changes_note`, responder hints and any pinned playbook artifact |
| `doc_review_comments` | 936 | Inline comments anchored by quote or heading path, with `resolved_in_revision` and the responder's disposition |
| `doc_review_attachments` | 912 | Image evidence per revision, stored under `<data_dir>/review-attachments/` |
| `doc_review_dispatches` | 965 | One row per adversarial dispatch: revision, intelligence class, focus, the reviewer task |

The allowed kinds, states and deciders are constants at `tables.py:830-832`. The states are
`in_review`, `changes_requested`, `rejected`, `approved` and `withdrawn`.
`ReviewService.OPEN_STATES` (`src/reviews/service.py:61`) holds the first three.

**The vault mirror.** Every revision is also written to the operator vault
(`src/reviews/vault.py`). The file path comes from the title at first submit:
`projects/<project>/<specs|plans>/<date>-<slug>.md`, with a numeric suffix on collision
(`candidate_paths`, `vault.py:45`). That path is stored once and never changes, whatever file
name the author passed to `--file`. Writes are atomic (`write_atomic`). The daemon compares
the vault file's body hash with the current revision, ignoring frontmatter (`vault_state`,
`service.py:843`). If someone edits the file outside the review, the daemon never overwrites
it. The review then refuses a decision with `vault_diverged` until the operator runs
`aq review import-edits` or the author revises.

**Lifecycle.**

| From | Event | To |
|---|---|---|
| — | submit | `in_review` at revision 1, with an open `review` gate |
| `in_review` | approve | `approved`; the gate is resolved and waiting tasks are recomputed |
| `in_review` | request_changes or fold_in | `changes_requested`; a revision task is filed; the gate stays open |
| `in_review` | reject | `rejected`; the gate stays open |
| `in_review` | decline | `rejected`; the gate is cancelled |
| any open state | revise | `in_review` at revision N+1 |
| any open state | withdraw | `withdrawn`; the gate is cancelled and waiting tasks are flagged `needs_attention` |
| `withdrawn` | reopen | `in_review`, with revision N copied forward as N+1 |
| `in_review` | import-edits | `in_review` at N+1, with the body taken from the vault file |

Every transition is a compare-and-set on the state and the expected revision
(`transition_review`, `src/database/queries/review_queries.py:331-346`). A lost race writes
nothing.

**Submitting.** `_cmd_review_submit` (`src/commands/review_commands.py:415`) creates the
review, its gate and revision 1 in one transaction, retrying up to three times if a
generated id collides. A worker may submit only from the task it holds. A revision task is
pinned to its own review: it may revise that review and no other (`:465`). The default
decider comes from the project's `review_delegate_to` setting (`:500-506`). Before a revision
is accepted, `_review_hygiene` refuses a body that is unchanged (`unchanged_content`) or that
only adds progress notes (`progress_only`).

**Notifying Jack.** `ReviewNotifier` (`src/reviews/notifier.py`) posts each new revision
once to the Discord channel, with a link to `/reviews/<id>` on the dashboard. It ticks every
30 seconds and uses `notified_revision` as its durable cursor. It skips reviews whose
decider is `supervisor` and reviews in paused projects. Discord buttons cannot decide a
review: an ask button for a review answers `ask.packet_required` (`src/asks/buttons.py:139`).
Decisions are made on the dashboard or in the CLI.

### 2.3 Deciding: the asks path

`aq review decide` still exists, but it is a compatibility wrapper. A decision that carries
feedback returns `ask.preview_required`, and the real path is `aq ask show`, then
`aq ask prepare`, then `aq ask decide`. The machinery is the human-asks system, owned by
`messaging-and-human-asks`. This system supplies the review adapter for it,
`ReviewAdapter` (`src/asks/review.py`).

- **Who may decide** (`authorize_review`): the local human always (a local CLI or the
  dashboard). A session may decide only if all of these hold:
  - it is an elevated, live, named session with profile `supervisor`;
  - it belongs to the review's project and its instance token matches;
  - the review's decider is `supervisor` or `user_or_supervisor`.

  Everyone else is told "Only the human or a live delegated supervisor may decide". The
  check runs again when the decision is applied.
- **What a decision freezes.** `validate` turns every comment and the overall note into a
  feedback item. An item counts as a prerequisite when its text matches
  `[blocking]`, `critical`, `must`, `security`, `race condition` or `before approval`.
  An agent comment with no classification also counts as one. By default a prerequisite
  is disposed `fold_in` and anything else `follow_up`. An approve that still carries a
  fold becomes an effective `fold_in`.
- **Durable application.** `reserve_review_operations` (`src/asks/operations.py:102`)
  writes one `review_feedback_obligations` row per item (`tables.py:7082`) and reserves the
  follow-on operations in `escalation_actions`: apply, file_task, playbook, mirror,
  review_event, notification, the gate and task events, and `ingestion`. `finalize`
  (`operations.py:457`) maps the decision to a state, checks that the revision or
  follow-up task was filed, resolves or cancels the gate, recomputes the waiting tasks, and
  queues ingestion for an approved spec or plan. Each operation retries on its own, so a
  daemon restart in the middle of a decision does not lose work.
- **The supervisor is told.** The notification stage sends a message from `review:<id>` to
  `supervisor-<project>`, with the subject `Review <id>: <decision>`.

### 2.4 Revision tasks and adversarial dispatch

**Revision and follow-up tasks** (`_file_review_feedback`, `review_commands.py:1400`). A
decision that needs a response files one task:

- `Revise <title> (review <id>)` for requested changes, or `Follow up on …` for an approval
  with open items;
- deduplicated on `review-<task_kind>:<decision_id>`;
- marked `_created_by_kind=review_response`;
- parented to the author task's parent if that parent is still open, otherwise filed at
  the project root.

The router picks the profile. A decision may hint a responder intelligence class but may
never name a profile (`routing.choice_forbidden`). The [aq-reviews
skill](../../../src/skills/aq-reviews/SKILL.md) tells the reviser to give every finding a
fixed, rebutted or deferred disposition and to resubmit with `--changes` and `--resolves`.

**Adversarial dispatch** (`_cmd_review_dispatch`, `review_commands.py:1178`). Only the
operator or an elevated session may dispatch. A dispatch:

- creates 1 to 10 tasks titled `Adversarial review: <title>`, with task type `research`,
  filed at the root and marked `_created_by_kind=review_dispatch`;
- uses intelligence class `deep-high` unless another is given, and refuses `--to`;
- excludes the author's provider when that provider is known (`:1243-1244`), so the
  reviewer runs on a different model family;
- is allowed once per revision unless `--force` is passed.

The dispatched reviewer may read and comment only on its pinned revision, and every comment
must be anchored with `--quote` or `--heading-path`. The opt-in cross-family loop of at most
three rounds is a recipe in the skill and guide. Generated contracts cover
`review_dispatch`, `review_show` and `review_decide`; caller authorization still applies.
A playbook cannot decide, and an ordinary `agent_task` is outside the dispatch ledger.
`review.dispatched` has no registered event schema. See the guide's playbook boundary.

**Attachments.** PNG, JPEG, GIF or WebP images up to 10 MiB may be added to the current
`in_review` revision before it is dispatched. They are served from
`src/api/review_attachments.py`.

### 2.5 Gating work on a review

`aq task create --after-review <review-id>` holds new work until a document is approved. It
also works with `--graph`, with `--from-spec` and on `aq task edit`. The server
(`_review_for_gating`, `src/commands/task_commands.py:3243`) refuses an unknown or withdrawn
review. If the review is not yet approved, it attaches each new task to the review's gate.
For a graph, the review is resolved before the graph is parsed (`:3447-3457`), so a bad
review id can never leave an ungated graph behind. Approval resolves the gate and the
tasks become ready. Withdrawal cancels it and flags them `needs_attention`. The graph
layout API shows the wait as `review_waits` (`list_review_waits_for_tasks`,
`review_queries.py:241`).

A common operator recipe is `aq task create --from-spec <spec> --root --after-review <rev>`.
It files the spec's fenced `aq-graph` block at once and gates every node on the review. That
recipe **does not go through spec ingestion**. There is no held batch, no fidelity review
and no check that the graph matches the document (§5).

### 2.6 Spec ingestion and held batches

[Spec ingestion implementation](../implementation/spec-ingestion.md) describes the
held-batch mechanisms and incomplete release wiring (§5). The approved design is vault
`specs/2026-10-13-spec-ingestion-with-task-structure-review-before-release.md` (review
`rev-bright-meadow`, approved at revision 2). Its implementation spec is vault
`specs/2026-10-14-spec-ingestion-held-batches-and-fidelity-review-before-relea.md` (review
`rev-agile-beacon`, approved at revision 1). What the audited checkout implements:

1. **Approval queues ingestion.** Approving a spec or plan queues the `ingestion` operation.
   `src/asks/ingestion.py` re-verifies the decision with `approved_packet()` (decision hash,
   review still approved and current, body hash, feedback obligations unchanged, follow-up
   tasks filed) and emits `spec.approved`. The standalone path `aq task spec-approve`
   (`src/commands/spec_commands.py:44`, local operator only) snapshots a vault spec without a
   review into `approved_document_snapshots` and queues the same operation.
2. **Policy files the ingest task.** The shipped `default-pipeline` playbook rule
   `spec-ingest-on-approve` calls `ensure_spec_ingest`
   (`src/commands/spec_ingest_commands.py:96`). It refuses session callers and creates one
   root task, `Ingest approved decision <id>`, for the `spec-ingest` role, deduplicated on
   `spec-ingest:decision:<id>`. The task requires the ingester to account for every goal of
   any document the spec supersedes as kept, changed or dropped. Every drop needs Jack's
   sign-off.
3. **The ingester proposes once.** The spec-ingest role calls `task_batch_propose`
   (`src/commands/proposal_commands.py:340`) a single time. It may only create tasks, and
   every root must be an epic or an operational root with a `delivery-of` dependency.
   Structure checks are in `src/task_graph/batch_checks.py` (also callable as
   `task_batch_validate`), and the graph digest comes from `src/task_graph/batch_digest.py`.
   - Missing `spec_kind` defaults to **design** in approved-packet loading.
   - For a **design** spec the server fixes the batch at two nodes: an epic plus one `deep-high`
     child that writes the implementation spec. It is released at once by
     `service:design_authoring_shape`.
   - For an **implementation** spec the batch is **held**. Every member task is created with
     `tasks.held_batch_id` set, the proposal is marked held, the `fidelity_review.dispatch`
     operation is reserved and the outbox event `batch.held` is queued.
4. **Held tasks are inert.** The predicate `not_ingest_held`
   (`src/database/queries/ingest_hold.py`) is applied everywhere a task could start or move:
   the claim frontier, push scheduling, launch, restart, settlement, the integration train,
   the delivery observer and the blocked-state computation.
   `tests/test_ingest_hold_admission.py` is the ratchet.
5. **Fidelity review.** `ensure_fidelity_review`
   (`src/commands/fidelity_review_commands.py:286`) creates one task for the
   `fidelity-reviewer` role per batch. It retries at most twice and dispatches at most three
   times; when those are exhausted the batch stays held and one ask is raised. The reviewer
   reads only its batch, edits it only through the held branch of `task_batch_update`, and
   records the outcome with `task_batch_review_record` (`:176`). A passing receipt moves the
   batch to `reviewing` and queues `batch.receipt_recorded`. A scope question moves it to
   `needs_scope` and raises an ask.
6. **Commit and discard refuse.** `task_batch_commit` on a held batch answers
   `fidelity_review_required`, and `task_batch_discard` answers `batch_retire_required`.
7. **Release.** The database primitive `ingest_batch_queries.release()` (`:648`) clears the
   hold under a revision and digest fence. On base no command calls it (§5).
8. **Recovery never releases.** Three reconcilers run on the daemon cycle under service
   principals: `spec_ingest_reconcile` (`src/orchestrator/core.py:3631`) replays failed
   ingestion operations, `batch_reconcile` (`core.py:3710`) re-emits the `batch.held` and
   `batch.receipt_recorded` wake-ups every 60 seconds, and `spec_ingest_legacy_inventory`
   lists proposals made before holds existed. None of them releases a batch.

The tables are `task_proposals` (`tables.py:1016`), `ingest_batches` (`:1036`),
`ingest_batch_members` (`:1085`) and `ingest_batch_receipts` (`:1106`). The live database
held no ingest batches on 2026-10-20.

### 2.7 Knowledge records

The user guide is [Knowledge records](../../guides/knowledge-records.md). The design is
vault `specs/2026-10-01-aq-work-and-knowledge-records-typed-records-links-and-versio.md`
(review `rev-fleet-cascade`, approved at revision 2; its body still reads "Status:
Proposed", and the plan notes that the approval record governs). The plan is vault
`plans/2026-10-01-aq-work-and-knowledge-records-implementation-plan.md` (review
`rev-quick-orbit`, approved at revision 1). It delivers the system in fourteen increments,
K01 to K14, and all fourteen are present in base.

The founding decision is a split: **tasks say what to do, knowledge says what we know.**
There is no knowledge task type. A record can motivate a task, and a task can produce a
record, but those links are informational and never feed the scheduler.

**Two layers.**

- `src/records/` is the domain-neutral core. It provides record identity
  (`identity.py`; a task's record id is a deterministic UUID from the installation id and
  the task id), authorization (`auth.py`), the base service with idempotency receipts and
  optimistic concurrency (`service.py`), links and their graph (`graph.py`), mixed
  task-and-knowledge search (`search.py`), the audit and export outbox (`outbox.py`), vault
  export (`export.py`), the task-to-record backfill (`backfill.py`) and retained source
  artifacts (`artifacts.py`). PostgreSQL triggers installed from `schema.py` make
  revisions, payloads and link versions immutable.
- `src/knowledge/` builds the knowledge domain on top. `KnowledgeService`
  (`src/knowledge/service.py`) combines create, update, retire and restore with authority,
  proposals, sharing and redaction.

**Data model.** The main tables are `records` (`tables.py:6023`), `knowledge_records`
(`:6049`, the current head), `knowledge_revisions` (`:6080`) with
`knowledge_revision_payloads` (`:6117`), the full-text projection `knowledge_search`
(`:6167`), link heads and versions, `record_requests` (idempotency receipts) and
`record_outbox`. Protection, import, context, extraction and deprecation add their own
tables. The full list is in [Database](../database.md#durable-knowledge--records).

**Writing.** Every write goes through a command, in one transaction:

1. `authorize_on` (`src/records/auth.py:58-182`) checks, in order: knowledge is enabled for
   the project; the principal holds the profile grant; writes are enabled; legacy memory is
   in `disabled` mode; and, for a worker session, that it holds the task at the given
   `claim_epoch`.
2. An idempotency key is bound to the hash of the arguments. A replay returns the first
   result, and a replay with different arguments is refused.
3. An update must name the revision it is based on (`if_revision`; `if_link_token` for task
   links). A mismatch answers `record.revision_conflict` with the current token.
4. The new revision is appended and the head advanced with a compare-and-set. The search row
   is refreshed and an `audit` outbox row is written, plus an `export` row when export is
   enabled.

Every edit resets verification to `unverified` and revokes any authority grant. Only the
author, or a supervisor, may revise a record, and a protected (verified) record can only be
changed through a proposal.

**Links.** There are six link types: `references`, `motivated_by`, `produces`, `supports`,
`contradicts` and `supersedes` (`src/knowledge/models.py`). The link matrix in
`src/records/service.py:515-583` limits which may join a task to knowledge and which join
knowledge to knowledge. A retired record names its successor with `supersedes`.

**Searching.** `aq record search` is mixed lexical search over tasks and knowledge, with
knowledge first. `aq knowledge list` searches knowledge only. Both use PostgreSQL full text
(`src/knowledge/search.py`). Retired and disputed records are excluded by default, and
records past `valid_until` or `recheck_at` are marked stale. The core has no embeddings: a
semantic retrieval provider may only re-rank and return references (`providers.py`), and
none is enabled.

`aq record capabilities` separates profile grants (`profile_granted_operations`)
from operations admitted by current project activation, live session authorization
and write flags (`granted_operations`). `unavailable_operations` maps excluded
grants to the service refusal code. Feature flags are reported separately; record
ownership, revision and write-claim checks still apply when an operation is used.
Workers cannot activate a disabled project through a profile grant.

**Protection.** Described in [knowledge protection](../design/knowledge-protection.md):

- *Proposals.* Anyone with the grant may propose a change. A supervisor accepts or rejects
  it, and a stale base is refused. Extraction output can only ever be a proposal.
- *Authority.* A supervisor verifies a record with named evidence and grants authority. A
  grant counts only while an approved document review contains the line
  `knowledge-authority: <record> <revision> <sha256>`. Authority is recomputed on every read
  (`src/knowledge/authority.py`). A verification badge alone never makes a record
  authoritative.
- *Global sharing.* Records are project-scoped. A global record is visible to a project only
  through an explicit share row, and only when `knowledge.global_enabled` is on.
- *Erasure.* `knowledge_redact` tombstones payloads permanently and queues purge work. It is
  restricted to the local operator (`src/knowledge/redaction.py:64-65`) and takes an
  exclusive advisory lock against all writers.

**Import and deprecation.** Legacy memory can be imported only through sealed manifests: a
read-only inventory, a hash-pinned dry run, then `aq knowledge import` apply, resume or
cancel in batches of up to 100, run by the local operator
(`src/knowledge/imports/`; [knowledge import](../../guides/knowledge-import.md)). Once a
legacy source is imported, a permanent per-source fence (`CompatibilityFence`,
`imports/compatibility.py:39`) refuses legacy writes with `memory.migrated_read_only`. The
fence holds even after a rollback. K14 deprecation (`src/knowledge/deprecation.py`;
[design](../design/knowledge-deprecation.md), [guide](../../guides/knowledge-deprecation.md))
counts compatibility use without recording content (`record_compatibility_usage`), reports
it through `aq doctor --check records.deprecation`, and checks removal evidence through
`adapter_removal_guard`. That guard hard-codes `removal_authorized: False`. Nothing in code
removes a legacy adapter.

**Rollout gates.** `KnowledgeConfig` (`src/config.py:794-880`) defaults everything off and
is hot-reloadable. The plan's gates G1 to G7 are operator decisions; none switches itself
on. The operator runbook is
[knowledge migration and restore](../../guides/knowledge-migration-restore.md). On this box
on 2026-10-20:

- on: `knowledge.enabled` for a named list of projects, `writes_enabled`, `ui_enabled`,
  `context`, and `memory.enabled`; `legacy_memory_mode` is `disabled`;
- off: `global_enabled`, `semantic`, `extraction`, `consolidation`, `export` and both
  `import` switches.

These historical flag values do not prove that operator gates were approved.
On that observation date, the live database held 26 knowledge
records with 48 revisions, 277 record rows in total, 45 link heads and 1,146 context
deliveries. It had no proposals, import runs, extraction jobs or citations.

### 2.8 Context delivery

The contract is [knowledge context](../knowledge-context.md) (K08 and K09). In short:

- **Preparing a bundle.** `ContextService.prepare_on` (`src/knowledge/context.py:247-373`)
  builds a bundle from exact pins first, then lexical discovery on the task title. It keeps
  to a hard UTF-8 byte budget (`src/knowledge/budget.py`), never truncates required
  instructions, and drops retired, disputed or out-of-date items. Omissions are reported
  without naming items the caller may not read. Bundles are cached for 300 seconds by
  request fingerprint in `knowledge_context_bundles`.
- **Recording delivery.** A bundle counts as delivered only when the receiving side
  confirms it with the bundle's own hash (`observe_delivery_on`, `context.py:415`). Only
  then are `injected` citations written.
- **Enablement.** `context_enabled` (`context.py:43-47`) needs `knowledge.enabled`,
  `knowledge.context.enabled` **and** `memory.enabled`. `memory` is a restart-required
  setting, while `knowledge` reloads live.

Three paths deliver bundles in production:

| Path | Where | How delivery is confirmed |
|---|---|---|
| Worker prime | `_cmd_prime`, `src/commands/surface_commands.py:457-550` | The CLI calls `knowledge_context_deliver` after printing (`src/cli/agent_surface.py:164-220`) |
| Task launch prompt | `src/orchestrator/context.py:345-371` | The bundle is set on the prompt builder; this block also clears the old L1 and L2 memory tiers |
| Supervisor start | `src/messages/session_lens.py:532-603` | Recorded after launch with transport `session_start` |

If the caller lacks a grant or knowledge is disabled for the project
(`record.forbidden` or `knowledge.disabled`), prime degrades: it prints without context and
warns which grant is missing. Prime never fails for that reason. The prime slot is titled
"Knowledge context" (`src/prime/sections.py:795`).

### 2.9 Legacy memory

The old memory layer stored per-project facts and notes in Milvus through the `memsearch`
library. The extracted [aq-memory plugin](../design/memory-plugin.md) and the
[extraction design](../../superpowers/specs/2026-04-25-aq-memory-extraction-design.md)
describe it. On base it is almost entirely inert:

- **The plugin never loads.** Core skips `aq-memory` and `memory` when loading plugins
  (`src/orchestrator/core.py:2556-2568`), and `check_legacy_plugin_load`
  (`src/plugins/registry.py`) refuses them. Lifting that needs the K07 versioned read-only
  handshake, which exists only as the unused ports `src/knowledge/memory_port.py` and
  `generation_port.py`.
- **Memory commands have a master pause.** When `memory.enabled` is false,
  `handler.py` refuses `memory_*` commands before dispatch with `MEMORY_PAUSED_ERROR`.
  Enabling it does not supply a legacy backend or lift the plugin-load fence. The
  [feature pauses](../design/feature-pauses.md) design explains why.
- **The memory tiers are empty.** The L1 facts and L2 context builders in prime return
  empty bodies (`src/prime/sections.py:773-792`), and the launch prompt replaces them with
  the knowledge bundle.
- **Reflection was deleted** (commit `735577659`). A profile's `## Reflection` section is
  prompt text only. [Reflection](../reflection.md) and
  [self-improvement](../design/self-improvement.md) are historical.
- **Consolidation is unscheduled.** Only a historical fixture playbook exists, and
  `MemoryConfig.consolidation_*` has no consumer. Its designated successor is K13
  knowledge consolidation, which produces proposals and is off.
- **Extraction and capture** (K13; `src/knowledge/capture.py`, `extraction.py`). The
  generation loop (`extraction.py:512`, started at `core.py:2544`) would turn task-close
  summaries into `hypothesis` evidence and then into proposals. It runs only with
  `knowledge.enabled`, `memory.enabled` and extraction or consolidation on. On this box it
  does not run.

[Memory scoping](../design/memory-scoping.md) and [memory
consolidation](../memory-consolidation.md) describe the older design and are mostly
historical (§5).

### 2.10 Command and doctor surface

| Surface | Commands |
|---|---|
| Reviews | `aq review submit`, `show`, `list`, `comment`, `decide`, `dispatch`, `withdraw`, `reopen`, `delegate`, `import-edits`, `attach`, `attachments`, `feedback-complete` (`src/cli/reviews.py` plus generated verbs) |
| Decisions | `aq ask show`, `prepare`, `decide`, `history`, `reconcile`, `retry-operation` |
| Ingestion | `aq task ensure-spec-ingest`, `approved-spec-packet`, `batch-propose`, `batch-validate`, `batch-update`, `batch-commit`, `batch-discard`, `batch-reconcile`, `ensure-fidelity-review`, `spec-approve`, `spec-ingest-reconcile`, `spec-ingest-legacy-inventory`; `aq system task-batch-review-record` |
| Gating | `aq task create --after-review`, `--from-spec`, `--graph`, `--root`; `aq task edit --after-review` |
| Knowledge | `aq knowledge create`, `show`, `list`, `update`, `history`, `diff`, `retire`, `restore`, `propose`, `proposal-decide`, `verify`, `authority-grant`, `authority-revoke`, `share`, `redact`, `import`, `export`, `create-task`, `cite`, `context-deliver`, `generation-status`, `generation-tick` |
| Records | `aq record search`, `show`, `capabilities`, `link-create`, `link-list`, `link-remove`, `repair` |

Every command is also `POST /api/<category>/<verb>` through the generated API, and an MCP
tool. Worker sessions are admitted to the review commands listed in `src/api/scope.py:153-160`
and the knowledge commands at `:84-104`. The fidelity reviewer has its own batch-scoped
admission (`fidelity_review_task_for_session`, `:618`). The dashboard has a reviews inbox and review page
(`dashboard/src/pages/reviews/`, `dashboard/src/panes/review/`) and a "Tasks & Knowledge"
project tab (`dashboard/src/pages/knowledge/`).

Doctor checks: `reviews.consistency` and `reviews.playbook_artifacts`
(`src/doctor/review_checks.py:22`); `records.task_mappings`, `domains`, `revision_hashes`,
`exports`, `links`, `index_lag`, `redaction_cleanup`, `outbox`, `deprecation` and
`pool_access` (`src/doctor/record_checks.py`). There are no `ingestion.*` checks yet.

Two skills tell agents how to use the system: [aq-reviews](../../../src/skills/aq-reviews/SKILL.md),
organised by which seat the agent is in (author, reviser, dispatched reviewer, decider), and
[aq-knowledge](../../../src/skills/aq-knowledge/SKILL.md).

## 3. Boundaries

| Neighbour | This system owns | The neighbour owns |
|---|---|---|
| `daemon-core` | The reconcilers, the review notifier, the record outbox and the generation loop as callables; the knowledge and review command mixins | Where they run in the cycle, start-up order, the command handler, the event bus, the schema and migrations |
| `task-graph` | The review gate's meaning, `--after-review`, held-batch membership and the `delivery-of` root rule | Task states, hierarchy, gates and `task_gates` as tables, graph parsing and creation |
| `scheduling-and-claims` | The `not_ingest_held` predicate and its ratchet | The frontier, claims and epochs; it applies the predicate |
| `routing-and-profiles` | Which role a review, revision, dispatch, ingest or fidelity task asks for, and the intelligence-class hint | Profile files, rung derivation and routing; `spec-ingest` ships, `fidelity-reviewer` is still missing |
| `projects-workspaces-git` | Nothing; reviewed documents are never committed | Workspaces and branches for the tasks this system files |
| `integration-and-delivery` | Holding batch members out of the train and the delivery observer | The train, delivery, the integration outbox that carries `batch.*` events |
| `playbooks` | The commands the `default-pipeline` rules call and the events they listen for | The playbook runtime and the shipped `default-pipeline`; playbook reviews pin compiled artifacts from it |
| `messaging-and-human-asks` | The review adapter, feedback obligations and the ingestion operation | The ask ledger, previews, decisions and Discord delivery |
| `supervisors-and-policy` | The delegated-decider check and the supervisor's knowledge verbs | When a supervisor decides, verifies, grants authority or accepts proposals |
| `sessions-and-harnesses` | Preparing and recording knowledge context | Prime rendering, launch and the harness transports |
| `providers` | Excluding the author's provider on dispatch; the generation provider settings | Provider identity, availability and usage |
| `configuration-and-vault` | The review mirror under `projects/<id>/specs|plans/`, knowledge export, and `KnowledgeConfig` and `MemoryConfig` semantics | Vault layout, the config loader and hot reload |
| `api-and-clients` | Review attachments and scope admission rules for its commands | Routes, auth and generated clients |
| `dashboard` | The data behind reviews, the knowledge tab and review waits | Their presentation |
| `test-service-and-jobs` | Nothing | Running this system's tests |
| `operations-and-recovery` | The review and record doctor checks; the knowledge runbooks | The doctor framework, backup and restore |
| `plugins-mcp-and-tools` | The legacy-memory load fence and compatibility fences in the notes plugin | Plugin loading, MCP tools and skill installation |

## 4. Principles and invariants

- **A decision is about one exact body.** A decision names the revision and its hash.
  Ingestion re-verifies the approved packet before it emits anything.
- **The database is the truth; the vault is a mirror.** The daemon never overwrites a vault
  file that someone else edited. A diverged file blocks the decision instead.
- **Only Jack, or a live delegated supervisor, decides.** Discord buttons, workers and
  dispatched reviewers cannot.
- **Feedback is frozen before anything is released.** Obligations are written with the
  decision, and finalize checks that the answering task exists.
- **Every state change is a compare-and-set.** This applies to review transitions,
  knowledge head advances and held-batch edits. A lost race writes nothing.
- **Held work is inert everywhere.** `not_ingest_held` sits in every admission path, and
  reconcilers never release. Release is an explicit, fenced act.
- **Reviewers stay independent.** A dispatch never names a profile, excludes the author's
  provider when it is known, and pins one revision.
- **Knowledge is history, not state.** Revisions are immutable. Restore copies an old
  revision forward, and erasure tombstones rather than deletes rows.
- **Authority is earned and re-checked.** It is computed at read time and bound to an
  approved review. Any edit clears it.
- **Generation proposes, people decide.** Extraction and consolidation can only write
  proposals.
- **Context is bounded and observed.** Required content is never truncated, and a citation
  is recorded only after delivery is confirmed with the bundle hash.
- **Fences are permanent.** A migrated legacy source stays read-only regardless of rollout
  flags, and nothing in code authorizes removing a legacy adapter.
- **Mechanism here, policy in playbooks.** Turning `spec.approved` into an ingest task
  is shipped policy. The intended `batch.held` to fidelity-review rule is still missing;
  the command mechanism alone does not dispatch or release that work.

## 5. Known gaps

The following source gaps remain at `52d2efe4c`. The dated task references in §6
are historical planning context; this audit does not re-query other tasks or
assert that their branch work has landed.

### Held ingestion is not wired end to end

1. The reviewed `default-pipeline` still contains only `spec-ingest-on-approve`,
   `proposal-ready-gate` and `commit-on-gate-resolve`. Nothing consumes
   `batch.held` or `batch.receipt_recorded`. Reconciliation re-emits the event;
   `ensure_fidelity_review` must currently be called explicitly.
2. No `task_batch_release` command exists. The database `release()` primitive
   in `ingest_batch_queries.py` is called only by tests. A review receipt does
   not make implementation tasks claimable.
3. No `fidelity-reviewer` profile ships. The role route and scoped review
   commands exist, but do not supply an executable profile.
4. The shipped `spec-ingest` profile still promises immediate commit and grants
   `task_batch_commit`, which refuses held batches. This is a shipped-agent
   instruction/policy mismatch, not permission to clear a hold.
5. No `ingestion.held_batches` / `ingestion.legacy_proposals` doctor checks or
   `aq batch` command group are present.
6. The manual `aq task create --from-spec ... --after-review ...` path gates
   approval but creates no ingestion batch, so it bypasses fidelity review and
   ingest goal accounting. It is a separate mechanism, not an equivalent
   implementation-ingestion recipe.

### Knowledge and context discrepancies

- Worker writes require the current claim epoch. The CLI injects it only for
  `knowledge_propose` (`src/cli/auto_commands.py`), while create, update and other
  worker mutations require explicit `--claim-epoch`. The guide and skill now
  document this workaround; the inconsistent CLI behavior remains.
- Supervisor launch records context transport `session_start`, outside the four
  transports declared in `src/knowledge/delivery.py`. `observe_delivery_on`
  validates only the transport string's length.
- The session-spec knowledge payload transports (hook envelope, start-up
  guidance file and memory-file pointer) have test coverage but no production
  caller passing a payload. Current delivery uses the paths listed in §2.8.
- Optional memory/generation adapter ports are defined but not installed by
  core. `memory.enabled` alone cannot provide a backend. `finding` and
  `research` are prose terms, not categories; use `reference` or `note` for
  research findings.

### Legacy surface still present in code

- `src/prompts/memory_revision.py`, `memory_consolidation.py` and
  `ProjectFactsheet` have no production consumers.
- The notes plugin's promotion/profile-revision paths still call missing
  `memory_store`; the files plugin's inspection path calls missing
  `memory_kv_set` / `memory_kv_list`. These legacy calls cannot save canonical
  knowledge. The `facts.md` change handler only logs.
- Stale reflection references remain in execution/reconciliation code. No
  reflection engine or periodic reflection policy is supplied.

### Review storage and automation

- `doc_review_revisions.playbook` and `doc_review_comments.heading_path` still
  use plain SQLAlchemy `JSON` rather than PostgreSQL `JSONB`, contrary to the
  repository's schema rule.
- `review.dispatched` is emitted without a registered event schema. Review
  commands do have generated contracts; missing contracts are not the reason
  the opt-in loop is manual. Caller authority and dispatch ownership still
  prevent treating an arbitrary playbook agent task as a deciding reviewer.

### Documentation reconciled by this audit

The reviews guide now covers current states, ask previews, frozen feedback,
completion evidence and held ingestion. The ingestion spec describes the
partial implementation. Knowledge context names `record_search`; the restore
runbook correctly restricts redaction to local operators. Historical memory,
consolidation, reflection and extraction designs carry explicit current
boundaries. Shared profile/README text distinguishes canonical database records
from editable policy and derived vault exports. These edits ship no missing
commands, profiles, policy rules, adapters or migrations.

The approved vault runbook `rev-crisp-beacon` was older than the repository
runbook in the 2026-10-20 inventory. This audit updates repository docs only;
it does not rewrite an approved review or claim the vault has been reconciled.

## 6. Planned work

Historical snapshot: task states were queried on 2026-10-20 when this section
was written. They are not a current queue view; source gaps above are verified
independently against the audited checkout.

### 6.1 Held ingestion (epics sharp-cascade-53, nimble-lantern-37, calm-orbit-69)

**Why.** Before holds, an approved implementation spec became a committed, claimable graph
as soon as the spec-ingest agent proposed it. Nobody checked that the graph matched the
document, and a wrong graph started running at once. The 2026-10-13 design
(`rev-bright-meadow`, approved at revision 2, revised by quick-ember-55) adds a fidelity
review before release. The 2026-10-14 implementation spec (`rev-agile-beacon`, approved at
revision 1, written by eager-cascade-72.1) splits it into phases P0 to P6.

| Phase | Item | State | What it changes |
|---|---|---|---|
| P0 | sharp-beacon-11 | completed, in base | Schema, the `delivery-of` dependency type, the hold predicate in every admission path |
| P1 | fresh-cascade-19 | completed, in base | Held batch creation and durable batch state |
| P2 | prime-forge-80 | completed, in base | `ensure_fidelity_review`, scoped held edits, `task_batch_review_record` and the scope ask |
| P3 | sharp-cascade-53 | in progress | Release and recovery |
| | .1 `task_batch_release` | completed on its branch, not in base | The fenced release command that makes a reviewed batch claimable |
| | .2 recovery, doctor, legacy inventory, downgrade guard, e2e S21 | paused | `ingestion.held_batches` and `ingestion.legacy_proposals` checks, a migration downgrade guard and an end-to-end scenario |
| P4 | grand-cascade-34 | completed | Post-release structure repair (root relocation, `task_split`, retirement with replacement); no `task_split` is in base yet |
| P5 | nimble-lantern-37 | in progress | Policy, profiles, pipeline and docs |
| | .1 `fidelity-reviewer` profile | completed on its branch, not in base | Ships the reviewer role |
| | .2 spec-ingest profile and `default-pipeline` rules | ready | Adds `fidelity-review-on-held` and `release-on-receipt`, rewords the spec-ingest profile and drops its commit grant; closes gaps 1 and 4 |
| | .3 docs | defined | Rewrites the spec-ingestion and review docs; closes gap 7 and part of gap 8 |
| P6 | calm-orbit-69 | in progress | API, dashboard and CLI |
| | .1 API models, batch endpoint, generated clients | completed on its branch, not in base | `GET` for a batch and the held state on task detail |
| | .2 dashboard held state and batch panel | completed on its branch, not in base | A held pill and a batch panel |
| | .3 `aq batch` CLI, doctor checks, MCP tools | defined | `aq batch show`, `validate`, `release`, `review-record` and `update` |

The critical path in the implementation spec is L0.1 → L0.2 → L0.3 → L1.2 → L2.2 → L3.1 →
L5.2. Once L3.1 and L5.2 reach main, an implementation approval will flow from ingest to
fidelity review to release without a person in the loop, unless the reviewer raises a scope
question.

### 6.2 Other open work that touches this system

| Item | State | What it changes |
|---|---|---|
| grand-zenith-64 — ingest the system inventory spec | in progress, paused | The ingest of `rev-bright-rapids` that produced the eager-vault-55 system specs |
| clear-impact-72.3 — `default-pipeline` rewrite | ready | Changes the shipped rules that turn `spec.approved` into ingestion; must keep nimble-lantern-37.2's rules |
| calm-grove-14 (H09) | ready | Part of the one-place-to-ask-Jack work that the review decision path runs on |
| vivid-pinnacle-60 (H08-OPS) | paused | Operator rollout for the same ask work |
| vivid-rapids-36 (H09-OPS) | ready | Operator rollout for H09 |
| eager-vault-55.19 — rewrite the vision spec from all eighteen system specs | defined | Consumes this document; the previous vision draft (vault `specs/2026-10-20-agent-queue-the-vision-and-the-system-map.md`, `rev-smart-nexus`) was withdrawn |

The ask work belongs to `messaging-and-human-asks`. Its design is vault
`specs/2026-10-11-one-place-to-ask-jack-cohesive-human-co…` and its implementation spec
`specs/2026-10-11-one-place-to-ask-jack-implementation-sp…`.

### 6.3 Knowledge rollout not yet planned

The knowledge plan's remaining gates are operator decisions, not tasks: G5 (a semantic
retrieval pilot), G6 (a paid extraction pilot) and G7 (deprecating legacy memory after two
releases, thirty days, reconciliation and a restore proof). The K14 evidence is in vault
`specs/2026-10-03-k14-synthetic-reconciliation-and-restore-rehearsal-results.md` (review
`rev-agile-glacier`, approved) and the repository report
[knowledge deprecation rehearsal](../../reports/2026-10-03-knowledge-deprecation-rehearsal.md).
The other K14 documents are the telemetry contract (`rev-bright-harbor`), the deprecation
guide draft (`rev-nimble-dune`), the operator guide (`rev-solid-ridge`), the updated
runbook (`rev-crisp-beacon`) and the earlier runbook
`specs/2026-10-03-knowledge-records-migration-restore-rehearsal-and-rollout-ru.md`
(`rev-bright-ridge`, approved at revision 2). All are approved. No task is filed for any of
G5 to G7 or for the gaps in §5 marked as having no task.

### 6.4 Recently completed work this spec reflects

- bright-willow-50: pool workers got `record.forbidden` on `aq knowledge list` although their
  profiles told them to use it.
- calm-current-60 and steady-forge-22: revision tasks now publish a new revision of the
  existing review instead of editing the vault file or opening a duplicate review.
- grand-delta-18: a withdrawn review no longer leaves its gate open forever.
- vivid-torrent-24: the fidelity reviewer's batch-scoped reads were restored.
- stark-forge-10.1, .2, .3, .5 and .7: the ask ledger, frozen feedback, immutable approval
  packets, admitted human input and the single "Needs you" query.
- keen-meadow-10, quick-flare-87 and quick-pinnacle-13: spec-ingest applies its graph, can
  close without branch delivery, and runs under its persistent role agent.
- prime-stone-95: paused projects no longer send review requests.
- stark-ridge-68: the reviews page shows a per-project pull-request summary.
- solid-ridge-41: the first research task to store its results as knowledge records.

## 7. Related documents and source

**Guides**
- [Reviews](../../guides/reviews.md)
- [Mini-projects](../../guides/mini-projects.md)
- [Knowledge records](../../guides/knowledge-records.md)
- [Knowledge import](../../guides/knowledge-import.md)
- [Knowledge deprecation](../../guides/knowledge-deprecation.md)
- [Knowledge migration and restore](../../guides/knowledge-migration-restore.md)

**Specs and designs**
- Document review design: vault `specs/2026-09-21-document-review-design.md`
- [Knowledge context](../knowledge-context.md)
- [Knowledge protection](../design/knowledge-protection.md)
- [Knowledge deprecation design](../design/knowledge-deprecation.md)
- [Spec ingestion implementation](../implementation/spec-ingestion.md) (stale, §5)
- [Database: durable knowledge and records](../database.md#durable-knowledge--records)
- [Feature pauses](../design/feature-pauses.md)

**Reference and reports**
- [Knowledge extraction provider](../../reference/knowledge-extraction-provider.md)
- [Knowledge retrieval provider](../../reference/knowledge-retrieval-provider.md)
- [Knowledge deprecation rehearsal](../../reports/2026-10-03-knowledge-deprecation-rehearsal.md)

**History** (historical records; the code is right where they differ)
- [aq-memory plugin](../design/memory-plugin.md) and
  [extraction design](../../superpowers/specs/2026-04-25-aq-memory-extraction-design.md)
- [Memory scoping](../design/memory-scoping.md)
- [Memory consolidation](../memory-consolidation.md)
- [Reflection](../reflection.md)
- [Self-improvement](../design/self-improvement.md)

**Source**
- Reviews: `src/reviews/` (`service.py`, `vault.py`, `diff.py`, `notifier.py`),
  `src/commands/review_commands.py`, `src/cli/reviews.py`,
  `src/database/queries/review_queries.py`, `src/api/review_attachments.py`,
  `src/doctor/review_checks.py`
- Decisions: `src/asks/review.py`, `src/asks/operations.py`, `src/asks/ingestion.py`,
  `src/asks/standalone.py`
- Ingestion: `src/commands/spec_ingest_commands.py`, `proposal_commands.py`,
  `fidelity_review_commands.py`, `spec_commands.py`, `src/task_graph/batch_checks.py`,
  `batch_digest.py`, `src/database/queries/ingest_hold.py`, `ingest_batch_queries.py`, the
  shipped `default-pipeline` in `src/prompts/reviewed_playbooks/`, and
  `src/profiles/defaults/spec-ingest/`
- Records: `src/records/`, `src/database/queries/record_queries.py`,
  `src/doctor/record_checks.py`
- Knowledge: `src/knowledge/` (including `imports/`), `src/commands/knowledge_commands.py`,
  `src/commands/contracts/knowledge*.py`, `dashboard/src/pages/knowledge/`
- Legacy memory: `src/knowledge/legacy.py`, `src/plugins/registry.py`,
  `src/plugins/internal/notes.py`, `src/prompts/memory_*.py`, `src/prime/sections.py`
- Skills: `src/skills/aq-reviews/`, `src/skills/aq-knowledge/`
- Tables: `doc_review*`, `gates`, `task_gates`, `task_proposals`, `ingest_batch*`,
  `human_asks`, `ask_decisions`, `review_feedback_obligations`,
  `approved_document_snapshots`, `records`, `knowledge_*`, `record_*` in
  `src/database/tables.py`
