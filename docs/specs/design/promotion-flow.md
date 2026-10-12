# Train and promotion policy entry

This entry maps the approved Git-first train design to its implementation and
operational documentation. The normative authority remains
[software-factory policy](../../concepts/factory-policy.md).

## Design authority and baseline

The operator-vault spec
`projects/agent-queue/specs/2026-10-06-git-first-train-fidelity.md`, §§2.2 and 4,
restores the prior process while preserving Git as the code-state authority.
Its §7 Q2 default is a PR pinned at S for each promotion. The companion
`projects/agent-queue/specs/2026-10-05-dev-branch-releases-revision-3.md`
defines the branch-chain schema, notes and deploy behavior. Where the older
spec describes `approve` as a local intent mutation, fidelity replaces that
with a GitHub review on the pinned PR.

The October 7 operator takeover combines the release mechanisms, reviewed
policies and cutover tooling. Live activation is scoped to agent-queue; rollout
to other projects and legacy retirement remain separate work.

## Fidelity crosswalk

| Fidelity section or block | Policy and documentation |
|---|---|
| §2.2 completion and collection | Retained completion refs, Git containment, immutable members and `AQ-Source` merges stay in the train. A worker close is a checkpoint, not delivery. |
| §2.2 epic completion and PR gate | Train opens the epic PR; root admission requires its exact source head and the boundary's checks. `authorized` does not require a human review; `reviewed` does. |
| §2.2 cadence | Root batches wait for quiet after the latest admitted input, capped from the first admission; defaults are 300/1800 seconds. Epic batches freeze immediately. |
| §2.2 candidate and repair | Candidate checks, attestation and expected-old fast-forward remain. Repairs follow bounded primary/debug policy and escalate according to `on_exhausted`. |
| §2.2 cleanup and controls | Git-proved delivery triggers separate cleanup. Pause/resume, eject and seal-now use preview/apply commands; see the [supervisor runbook](../../guides/git-first-train-runbook.md#supervisor-controls). |
| B1–B5 (D1–D3) | Flow schema, activation, promotion targets and pinned-PR intents are shipped. See [promotion flows](../../guides/promotion-flow.md). |
| B6–B8 | Annotated-tag selection, deployment records, additive-migration tooling and custom-archive restore are shipped. See [releases](../../guides/releases.md). |
| B9 | Prepare tasks, daemon notes inputs, digest/version/additive-migration request checks. |
| B10 | Hotfix filing, default-branch backmerge sources, intermediate fast-forward intents and per-target ledger guards. |
| B11 | Chain/tag rulesets, remote protection diagnostics and workflow triggers. |
| B12 | Reviewed request/continuous policy bundles, resolved by stored step type; inactive until project activation. |
| B13 | Maintained promotion-flow and release guides, factory policy and repository map. |

Sources for shipped train behavior:
[src/integration/train_sources.py](../../../src/integration/train_sources.py),
[models.py](../../../src/integration/models.py),
[cleanup.py](../../../src/integration/cleanup.py), and
[promotion_steps.py](../../../src/integration/promotion_steps.py).
The [repository map](../../contributing/repo-map.md#train-fidelity-and-release-modules)
locates the added modules and marks later deletion as planned.

## Implementation boundaries

The new train and release flow use retained Git readers, ownership guards and
shared contracts. Legacy publishers and subject runtimes remain available only
for legacy operation and recovery; active train startup does not construct them.
Shared definitions have one retained owner, with compatibility imports for old
callers until retirement.

Delivery availability is checked against the selected runtime, rather than the
presence of legacy publisher objects. An active train serves `train`, `hierarchy`
and `development`; legacy subject delivery requires active reconciler visits.
`disabled` and `observe` deliberately have no writer. A delivering project whose
selected engine is unavailable, or whose designated repository is missing or
belongs to another project, is reported by `integration.delivery_engine` and the
`stall.sweep` patrol. Offline doctor uses startup configuration; daemon diagnostics
use the selected service when available. These checks never migrate operator policy.
Commands refuse enabling a mode with no selected engine, while still allowing an
operator to disable integration. Task explanations name `delivery_engine_unavailable`
and say "no delivery engine for this project" for undeliverable prerequisites,
instead of describing their delivery as ordinary pending work. Git proof remains
the authority for dependency admission.

The reviewed policies request, gate, publish and backmerge through commands.
They do not publish GitHub Releases or dispatch deploy hooks. Keep those optional
`after` settings disabled for this rollout. Release tags and operator deployment
selection are described in the [release guide](../../guides/releases.md).

Live activation configures the default branch and reviewed project policies;
release requests still require their configured exact-head approval. The first
live release and cross-project rollout are tracked separately. Legacy retirement
remains pending operator testing and the observation gate.

Continuous steps also observe their source on each train visit. An enabled,
reviewed project activation receives a durable `promotion.source_settled` event
once the current source is ahead of the target and its exact-head push checks
are green under committed default-branch trust. Pending checks are observed again
on later visits, including after restart or activation without a new settlement.
Notifications are deduplicated by repository, source, step configuration and
activated artifact. The reviewed policy still owns requesting and superseding
intents; observation never publishes a branch or bypasses approval. Private PR
heads are created under the full forty-zero absent-ref lease; publication uses
the observed full target OID.

The active Git-first service dispatches the five `promotion.*` policy event
types through the durable outbox. Legacy integration events remain undelivered
in that mode, so enabling promotion policy never reactivates a legacy writer.
Both the outbox page and its scan boundary apply the same event-type filter.

`promote_repair` lets an operator or project supervisor back up and restore a
stranded daemon-owned promotion task to `IN_PROGRESS` with no worker route.
This route-reset exception never selects a worker profile; the mandatory-routing
ratchet records it alongside other route resets.

Concurrent train and promotion-policy check refreshes share the cache key
`(repository_id, sha, check_name, producer_id)`. Insert attempts use distinct row
IDs so the cache-key upsert handles their race; updates retain the stored row ID
and continue to reject older observations and run attempts.

`integration.continuous_promotion_lag` and `stall.sweep` report active continuous
steps with source-only commits older than six hours. Age is the oldest
unpromoted first-parent source commit's committer timestamp, so continued source
advancement cannot hide accumulated drift. Reports include exact source/target
heads, ahead/behind counts and the threshold. Missing Git evidence is reported
as unavailable, never as a healthy flow. These diagnostics read Git and stored
configuration; they do not request or publish promotions.

Promotion source admission may also use the committed manifest's optional
`check_sets.promotion-source-audit`. It explicitly lists the full check names
produced by the delivery-branch audit, `unattested-ci / <required name>`, for
every check in the selected step's required set. This alternative applies only
when every canonical required name is absent from a completed push run. A
partially present canonical set or a real canonical failure cannot be replaced
by audit success. The complete audit set must independently pass the existing
exact-head, push-event, producer App and workflow-attempt checks. Missing,
pending, failed or untrusted audit evidence still refuses admission.

The source audit does not authorize publication or supply candidate evidence.
Candidate checks, promotion attestation, private pinned PR CI and release
approvals continue to require their configured exact names. Enabling the
alternative requires committing its full named check set to the designated
default branch and running daemon code that understands source admission.

Promotion source admission may retain the newest remaining check for a name
when newer checks on the same SHA belong to a completed cancelled or skipped
`aq/batches/` push workflow, including jobs that succeeded before cancellation.
Every observed attempt of that batch suite must
be cancelled or skipped; a pending, failed or malformed attempt prevents this
exception. Cancellation without alternative evidence still refuses admission.
The remaining checks must pass the existing exact-head, producer App, push-event
and latest-attempt validation. Real failures and unfinished checks remain
blocking, including those from batch branches. Candidate and attestation gates
continue to select the newest check without this source-only exception.

## Trust-manifest bootstrap without hierarchical routes

`integration_trust_manifest` accepts an optional `hosted_check_policy` object
instead of a full hierarchical `policy`. The CLI reads it from
`--hosted-check-policy FILE`; supplying both inputs is refused. With neither,
the existing bound hierarchical policy remains the default. App verification
continues to require the full policy.

The hosted document contains only `version`, ordered `names`, `producer_id`
and optional `check_sets` (a mapping of names to ordered check-name lists).
Unknown fields, non-string values, empty or whitespace-only strings, empty
check lists, duplicate checks, and noncanonical numeric producer strings are
refused. Names and version are preserved exactly, without sorting or trimming.
The producer is a positive decimal App ID string without leading zeros.
Explicit named sets replace matching committed sets; other valid committed
sets are preserved. Duplicate JSON fields in the CLI input are refused.

The repository still belongs to the requested project and its numeric ID and
full name come from the authenticated binding. The attestation App ID comes
only from the daemon client's credential identity. Missing identity, login
credentials, a mismatched binding or identical producer/attestation Apps fail
closed. The hosted document cannot supply these identities or routing data.
Stored promotion attestation names and the exact-SHA committed comparison
remain shared with the full-policy path.

This read-only bootstrap works before binding hierarchical routes, including
for a development-mode project. It never binds policy, changes integration
mode, activates a promotion flow, deploys code or bypasses release/operator
gates. Existing required checks, including native platform checks, must be
copied into the hosted input without substituting a different check set.
