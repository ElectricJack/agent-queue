# Supersede a stored passing completion with a live Subject

A retained passing train root can remain blocked when its old completion
pipeline contains `pr_merge`: current train policy refuses that command, while
ordinary `task close --obsolete` refuses a live integration Subject. Root
authorization requires a completed task and does not settle this state.

Use `aq integration supersede-completion` from the local operator or a live
named supervisor for the task's project (the global supervisor must select the
project explicitly). Workers, setup-worker grants, playbooks, stopped
supervisors and cross-project supervisors are refused. The supervisor profile
must grant `integration_supersede_completion`; shipped grants are synchronized
on daemon start/profile reload. Installing source alone does not establish that
the daemon has loaded it. This procedure does not restart or deploy a daemon.

Before preview, the task must be unassigned and explicitly paused, blocked or
completed, with a uniquely latest passing completion. Stop a retained writer
through the existing supported session/task pause controls. The provider must
freshly confirm the exact session name, provider and instance token stopped.
Never replace that proof with a stored status or an invented human flag.

The authorized caller must read the original human directive and determine
that it supersedes this exact root. `--human-reply` identifies an existing
inbound `escalation_messages` row with a server-verified `human:` actor and its
supervisor inbox delivery, bound to an escalation for this project/task. A
supervisor's report about a directive is insufficient. The command retains
the entire original reply and the caller's interpretation. It neither creates
another escalation nor resolves the incident, answers a gate or requests new
product/CI approval. If the original verified reply is unavailable in this
form, report that evidence gap to the incident owner; do not fabricate or
re-submit a directive to make the guard pass.

Read the current task, completion, Subject and existing escalation using the
supported read surfaces. Supply their exact identities:

```bash
aq integration supersede-completion ROOT_ID \
  --project PROJECT_ID --subject SUBJECT_ID \
  --expected-version SUBJECT_VERSION --expected-claim-epoch CLAIM_EPOCH \
  --completion COMPLETION_ID --head FULL_SOURCE_OID \
  --human-reply VERIFIED_REPLY_ID \
  --reason "Original human directive supersedes this exact root" --json
```

Preview defaults to `--dry-run`. It returns `proof_sha256` and the full proof.
Review the proof and repeat **the same arguments** with:

```bash
  --proof PREVIEW_SHA256 --apply
```

The proof covers project integration generation/mode/repository and URL;
task status, claim epoch and branch; Subject id, version, natural key, kind,
phase, policy pin, generation, head/base and writer lease; the uniquely latest
completion's id/outcome/commits/PR; current parent episode, checkpoint version
and operation/stages when present; materialized source origin; canonical owner
id/role/ref/fences/attachments; retained checkouts and stopped session instance
identities; original verified reply; and fresh remote branch plus immutable
Git completion retention. Apply repeats the proof under nonblocking exclusive
repository/parent guards and hierarchy/row locks. Any changed digest requires
a new preview. An active writer operation refuses promptly; retry after its
owner has stopped or settled it.

Only source or parent-episode Subjects in train mode are supported. Parent
operations with delegates or unreleased collector owners must settle through
their owning recovery first. Other active Subjects on this task/source, open
task/Subject gates, open batches/children, unknown or live writers and
unresolved writes/cleanup refuse. This control cannot waive those guards or
cancel a root batch, other member, delegate or project schedule.

Apply atomically closes the selected Subject and cancels its exact unowned
parent operation/stages when applicable, then invokes normal obsolete close.
The task is recorded as completed with `work_outcome=abandoned` and obsolete
metadata. No new passing or no-change completion is added, and original
completion/PR/source, policy budgets, episode and checkpoint history are
preserved. The append-only Subject journal records the proof, authenticated
executor and reason. A later obsolete-close refusal rolls back the entire
settlement.

For a managed parent, an internal transaction-bound settlement receipt allows
this abandoned obsolete transition. It cannot be supplied through command
arguments or reused for ordinary verified completion or root authorization.

Normal fenced owner preservation and PR/branch retirement run after commit.
`cleanup.state=pending` means settlement succeeded but cleanup remains;
inspect its exact reasons and let existing cleanup controls retry. Source
provenance continues retaining the original source even when normal cleanup
removes the task branch. Repeating the same applied proof returns
`already_superseded` and retries cleanup, without another journal/completion.
Repeating with `--dry-run --proof PREVIEW_SHA256` only inspects the receipt;
it does not retry cleanup.
Changed replay arguments, reopened tasks or changed settled identity refuse.

Disposable PostgreSQL/Git regressions prove the source implementation. They do
not prove another project's current state or loaded deployment. Obtain an
owner's runtime preview/apply/cleanup receipts before claiming that incident
recovered. Missing receipts are a bounded evidence gap, not authorization to
operate that project or wait indefinitely for it.
