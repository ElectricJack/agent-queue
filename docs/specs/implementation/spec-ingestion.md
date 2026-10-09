---
spec_kind: implementation
status: draft
---

# Approved document ingestion

`ReviewService.decide` emits `spec.approved` after a successful approval of
kind `spec` or `plan`, using the absolute vault path after its status rewrite.
Other documents and rejected or stale decisions do not ingest. The default
pipeline deduplicates on `spec-ingest:<path>` and runs the role at deep-high.

Submitted `spec_kind: design | implementation` belongs to each immutable
review revision and survives vault regeneration. Missing values remain unset
for legacy documents; the ingest agent reads the content before classifying.
Design documents produce one epic containing one deep-high design task to
write a code-grounded implementation spec and submit it to Jack's review.
Implementation documents produce phase/deliverable epics and self-contained
children, with dependency edges between leaves only. Structural parent-child
edges are the exception. Routing consumes type/class hints, never profiles.

`task_batch_propose` applies the graph as one transactional change set when
the caller is a live spec-ingest role assignment, so an approved document
reaches the graph in the call that stages it: the proposal row, the epics,
children, routing gates, provenance and edges commit together, and the call
returns the created task ids. There is no proposal gate and no separate
commit call; a later `task_batch_commit` on that proposal is an idempotent
replay returning the original receipt. Only an authenticated live spec-ingest
role assignment may produce such an ungated proposal. Its source must match the held task's dedup path and an
approved vault document. The server stamps this authority into the proposal;
caller-chosen source text cannot bypass ordinary proposal approval. Such
proposals emit no `proposal.ready`, so the human proposal gate does not run.
Ingest authority only creates work: a change set carrying edits, edge removals
or comments on existing tasks is refused, and such updates go to the supervisor
as an ordinary, human-approved change set. A failed ingestion leaves no
proposal behind to commit by hand.

Ordinary proposals keep the staged flow: `task_batch_propose` persists a
`ready` proposal, `proposal.ready` requests approval, and the human gate's
resolution is what `task_batch_commit` accepts as authority.

The proposal status, epic/child rows, routing gates, provenance and edges
commit in one transaction, including projects without hierarchical delivery.
No task-created event fires before that transaction commits. Validation
rejects flat roots, duplicate parents, container dependency edges and cycles.
Mandatory routing/dependency promotion determines the initial READY frontier.

Acceptance: review approval and dedup through the recorded default pipeline;
revision classification round trips and vault recovery; recorded design and
implementation proposals through a live role principal produce live tasks
with no commit call, and replay returns the same receipt; ordinary proposals
are still staged and gated; fault injection and a second database
connection prove rollback and absence of partial graph visibility.

Rollout: revision `a00000000081` adds nullable revision classification.
The operator migrates the database. Shipped profiles are write-if-absent;
the operator reconciles the installed spec-ingest Role and grants while
preserving its chosen harness. Rebuild and ship all reviewed pipeline copies.
