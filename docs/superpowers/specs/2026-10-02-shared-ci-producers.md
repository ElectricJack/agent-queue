# Shared CI producers and primitive adapters

Implements `rev-agile-ridge` revision 2 §3.4 primitives 8–10 and §5.6 for
task `agile-harbor-62.2`, against the `vivid-willow.1` subject contracts.

`CIAdapters` binds `ci_request`, `ci_observe` and `ci_attest` to
`PrimitivePorts`. The integration owner injects a producer resolver derived
from the subject's pinned policy. Registration is explicit; importing these
modules neither enables the reconciler nor changes an existing project's engine.

Both producers return `ProducerObservation`, containing the same exact head,
required-check version, producer identity, CI state and classification. Only
trusted, conclusive green/red evidence counts as an attempt. Cancelled,
superseded, pending, missing, untrusted and infrastructure results retain their
classification and reason. Repeating an observation preserves its evidence
identity. The adapters append replay-safe action evidence to the subject journal;
counting an attempt remains the separate `record_attempt` primitive.

Hosted observation reuses `AuthenticatedGitHubObserver`. It keeps frozen
repository/producer identity, newest workflow-attempt coverage, expected event
and exact-head checks. App-mode observation requires `IntegrationTrustManifest`
and a canonical numeric producer; policy-only trust cannot substitute for the
subject tree's manifest. Hosted request delegates to an injected existing
journaled snapshot publication or workflow dispatch command. It never performs
an unfenced push itself. Attestation delegates to `IntegrationAttestationService`,
including its fresh authenticated observation, manifest checks, prewrite journal,
lease and read-back. A local pass does not create an App attestation.

Local request submits finite validation commands through `PublisherJobs`, using
the internal integration submission path. That path provisions a disabled,
detached clone at the exact SHA, never runs in the publisher's retained clone or
a worker's workspace, and persists jobs across daemon/caller restarts. Commands
run in order, sharing one snapshot per validation request so installation steps
can supply ignored dependencies for subsequent checks. Request keys bind the
subject, head, generation, policy artifact, validation plan and explicit attempt
id. The policy supplies a new attempt id to retry infrastructure; observing or
replaying an existing request never starts another attempt. Observation is a
bounded database read, with no polling wait, job cancellation or shell fallback.
Empty validation is `none`, never fabricated green.

Local terminal evidence requires the recorded integration owner, exact input
SHA, snapshot mode, stable input, matching immutable result hash, successful
job state and observed zero exit for a pass. Failed, cancelled, lost, modified
or malformed producer results cannot become green. An actual failure outranks
an infrastructure result when aggregating completed checks.

Before any adapter action, the durable subject must still match the supplied
head, generation, policy and version, be live, owned by the reconciler, and
have no human gate. It is checked again before recording observation evidence;
late results remain superseded facts and cannot count for the new head.

Focused verification: `aq test tests/test_integration_ci_producers.py`.
Affected area: existing hosted CI, attestation, development validation and
managed-job snapshot tests. No production migration or rollout is performed.

Operator handoff: construct the producer resolver from the pinned policy and
bind these adapters in the integration owner's registry. Keep feature-off
ownership on the legacy engine until the enclosing phase's evidence and approval
gates pass. Jobs admission (`resources.jobs.enabled`) is required for new local
requests; disabled admission returns unavailable, without a shell fallback.
