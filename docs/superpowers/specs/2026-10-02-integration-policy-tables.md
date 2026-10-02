# Integration policy tables (phase 1)

Task `vivid-willow.4`, review `rev-agile-ridge` revision 2,
SHA-256 `5a3ef472bebf25cf4d308842488837646defdb26994cc87990158717fb5fb928`,
§3.5–§3.7. This is the policy/compiler component. It does not create subjects,
run the reconciler, or change a live activation.

## Artifact and authoring contract

A V2 Markdown source may contain exactly one fenced `integration-policy` JSON
block. The proposal compiler takes this literal block from the author's source;
a compiler agent cannot replace it with a different table. Duplicate keys,
unterminated blocks and malformed tables are errors. The optional
`PlaybookDefinition.integration_policy` field holds the compiled table. Omitting
it preserves existing artifact serialization and hashes.

The policy contains `max_wait_seconds` and `tables` keyed by subject kind.
Each table contains ordered `cases` (`rule`, V2 `when`, named `action`), named
`actions`, a bounded-wait `default`, and optional pinned `required_checks`.
First true case wins. Conditions and inputs use the existing V2 expression
language, limited to `s` and `subject` bindings; unknown observation paths and
bindings are rejected. An action names exactly one of the foundation's twenty
primitives and its typed input fields. Every outcome, including `unknown`, has
an explicit schedule. Outcome routes do not call further primitives.

The compiler checks:

- Complete, closed outcome sets and required input fields.
- Finite positive waits and ordered backoff ceilings within `max_wait_seconds`.
- A bounded-wait default and an explicit positive `s.wait_overdue == true` case.
- A first `s.held == true` case that waits and preserves the current phase.
- Human gates with a named question, distinct choices and either a valid timed
  default or explicit `no_default: true`. Created/reused gates must hold.
- Complete answer schedules for every gate choice. An explicit hold answer
  cannot become an automatic retry, and replay retains the original timeout.
- Every action target exists and every declared action is used.

Table changes contribute to executable review diffs and are displayed through
the existing rule field-change projection. The generated V2 JSON schema
describes the optional field; no new command or API surface is registered.

## Evaluator contract

`src.playbooks.integration_policy.CompiledIntegrationPolicy(definition)` takes
an already verified immutable artifact. It exposes synchronous, pure methods:

```python
evaluate(subject, facts) -> Decision
schedule(subject, decision, outcome, *, now) -> SubjectSchedule
phase(subject, decision, outcome) -> SubjectPhase | None
```

The evaluator verifies the subject's exact artifact pin and observation
identity/version/phase. It binds `s` to `SubjectFacts.binding()` and `subject`
to the durable row. Derived projections supply `tested_head`, typed
`merge_members`, and `next_writer_ordinal`; they perform no live lookup. Resolved
inputs are validated by the foundation's primitive argument model. Decisions
carry the matched rule, facts digest and pinned artifact identity. Scheduling
checks decision/outcome identity and clamps its wait to the subject's immutable
bound. A held route requires the adapter's exact `gate_id`; a reused timed gate
also requires its persisted `timeout_at`. Progress on an answered gate retains
its identity with an immediate visit so the table applies the answer.

`IntegrationPolicyFacts` is a compatible typed extension of `SubjectFacts`
with optional `publisher_fence: Fence`. Its inherited binding and digest include
that exact observed authority. A publisher is distinct from a repair task's
lease: the evaluator never derives publication authority from `writer`.
Plain foundation facts remain accepted; the root table waits with a bound until
the observer/adapter supplies this field. A fence for another repository/ref is
refused. The shared foundation module is unchanged.

The reconciler must load by `subject.policy.artifact_sha256`, rather than the
current project activation. The foundation's database trigger prevents repinning
an existing subject and artifact collection retains both subject and journal
pins. A new activation applies to new subjects only.

## Shipped policy and operator handoff

Both `agent-queue-root-train` and the generic `root-train` reviewed bundles
contain a table alongside their existing event rules. Sources remain disabled.
The tables preserve reviewed/source-CI admission with authorization read as
data; the current one-live-batch behavior (`seal.busy` backs off); exact-green
expected-old fenced publication; complete-batch repair; primary 1800 seconds/3
attempts and successor 3600 seconds/3 attempts; unclaimed capacity waits;
stopped-writer proof; rebuild on base movement; and cleanup retaining failed
work for seven days. Parent failed-child policy remains `block`.

Agent Queue retains its previously authorized continuation. The generic
template defaults exhaustion to a named no-default human gate. No-progress
holds remain explicit in both variants, and authorized successors use the
observed next ordinal. Editing/copying
the table does not activate alternatives or authorize ejection.

The existing observer/adapters must provide exact repository publisher lease
facts through `IntegrationPolicyFacts` (or promote its optional field into the
shared foundation) and enforce primitive publication authority. A missing or inconsistent
fence is not fabricated by the policy evaluator. The root adapter task owns
that connection, and the reconciler owns atomic phase/schedule persistence.

Before production cutover, the operator must review/import the exact bundle,
collect the specified shadow-week and real-Git/PostgreSQL evidence, then enable
exclusive reconciler ownership of root subjects. Re-enabling legacy rules is
the feature-off rollback. This worker neither imports a live bundle nor changes
the daemon or the operator database. Existing `keen-stone-14` recovery remains
owned by its existing task.
