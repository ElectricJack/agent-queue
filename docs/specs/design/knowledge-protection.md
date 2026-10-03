# Knowledge protection (K05)

This implements approved plan `rev-quick-orbit` revision 1, sections 4–7 and
11–13. Knowledge is independent of executable tasks and document reviews.
Deployment leaves all knowledge, memory, import and provider switches unchanged.
The local operator owns schema upgrades and feature activation.

## Proposals and verification

`knowledge propose` accepts an active, unverified snapshot, exact `if_revision`
for an existing record, optional bounded link operations and an idempotency key.
The returned proposal hash binds the target, base and entire proposed snapshot.
Workers may propose corrections to readable project records; they cannot decide,
verify, grant authority or revise a protected current revision, even if a profile
accidentally grants those command names. An explicitly narrowed
`knowledge-extraction` service principal may only propose in its project.

`knowledge proposal-decide` requires supervisor/operator authority, a reason,
the exact proposal hash and base. It locks the proposal then its source. A
changed base records `stale`, with no merge or new revision. Concurrent acceptance
and retries return one resulting revision. Rejection retains the proposal until
administrative erasure. New-record proposals allocate identity at acceptance.

`knowledge verify` creates a revision with named evidence and server actor/time.
Only this command can set verified or disputed status. A subsequent content,
source, link, retirement or restore change clears verification and revokes the
current authority grant. Historical verification remains attached to its revision.

## Policy authority

An authority grant annotates one exact current, active, verified revision.
`knowledge.authority_review_required` defaults to true. The retained approved
review must belong to the same project, have the supplied current revision/hash,
and contain this exact standalone binding line:

```text
knowledge-authority: RECORD_UUID REVISION_UUID CONTENT_SHA256
```

The grant transaction locks the review row. Reads and idempotent grant replies
recheck review state; changing/withdrawing its approval removes the annotation.
This never approves another review, activates a playbook or overrides instruction
files. Where the operator explicitly disables the review requirement, a privileged
command and reason are still required. Global reviews do not have an existing
review-domain representation; a project review cannot authorize global policy.

## Global scope and sharing

Every transport selects either `project_id` or explicit `global_scope=true`.
Missing scope is an error. Global commands require `knowledge.global_enabled`;
a global supervisor also needs the operation capability and `knowledge_share`.
Project elevation alone never grants global access. Installed grants are not
reseeded by this implementation.

Global records are private until an explicit per-record share. Project callers
can read individually shared globals, search them and link them from project
knowledge; they cannot edit or propose against them in the project scope.
Cross-project grants are refused. Global sources may reference only retained
global artifacts in v1; task/review/git/URL sources cannot establish global
visibility. Share validation checks all served history and outgoing targets for
the recipient. Later global edits recheck every share. Revocation is applied to
search, history, links and export on the next read. No denied title, snippet or
count is returned.

## Erasure and recovery

`knowledge redact` is local-operator only. It defaults to dry run; applying it
requires `dry_run=false`, a new idempotency key, the exact current revision and a
reason code (`sensitive`, `privacy`, `operator_erasure`). It selects one revision
or the whole record and reports the closure of affected revisions/records.
Derived summaries, link metadata versions and retained artifact references may
expand that closure. Preview before applying to understand that expansion.

Redaction exclusively fences knowledge writes while computing that closure;
ordinary writes share the fence. Task/claim/integration paths never use it.
It atomically creates a permanent ledger and hash tombstones, nulls selected
payloads, scrubs proposals/link metadata and search, revokes authority and
invalidates exporter leases. Reads, request replay, restoration and same-scope
reimport of identical canonical bytes refuse redacted material immediately.
The database guard permits only this one-way audited transition.

High-priority purge intents run even when knowledge/export flags are disabled.
The purge uses the same per-record filesystem lock as export, removes managed
exports and crash-left staging files, and erases retained artifacts only beneath
the vault's `record-artifacts/` namespace. Missing files are successful retries;
path/symlink/lock errors retain the intent. Optional semantic indexes are absent
at this increment; later providers must implement erasure acknowledgments before
activation. No cached context bundles exist before K08. `records.redaction_cleanup`
reports incomplete ledger cleanup as well as stale export/search projections.

Backups remain access-controlled operator artifacts. A restore must remain in a
separate disposable environment with every knowledge, export, context and
provider read/delivery path disabled until the complete post-backup redaction
ledger has been reapplied and cleanup verified. K05 does not expose a restore or
import bypass. Restoring an older backup without the complete erasure ledger is
unsupported. Already exported human copies cannot be recalled by this system.
No destructive downgrade is allowed once knowledge or protection data exists;
rollback disables writes and retains records, receipts and erasure evidence.
