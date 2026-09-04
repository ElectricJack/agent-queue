---
playbook_id: pr-merge-sweep
artifact_sha256: "sha256:8b1c7bec5aee1aa4d864d75e203a581a2f8289cbe6a5847b442c545e515d2525"
source_sha256: "sha256:38c3f724c0f68fa7039118393c97d3ed11c72bcb58f0fc045bb7a4284600b8d7"
contract_fingerprint: "sha256:7d1dab70ae2d72185eace7dedc3836b1bdfeed3ce168936036372ee8e059aaf7"
reviewed_by: null
reviewed_at: null
decision: pending_operator_approval
questions_resolved: 2
capabilities_granted:
  aq_commands: [ensure_task, task_route]
  harness_tools: []
  plugin_tools: []
profiles_referenced: [pr-merger]
---

# Pending V2 review record — `pr-merge-sweep`

This is a staged operator-review record, not an approval. No code reads this
file as a capability grant, and `decision: pending_operator_approval` excludes
it from approved-fixture discovery and from an activation import. The operator
must verify the artifact hash above, replace `reviewed_by` and `reviewed_at`,
and set `decision: approved` only after reading the semantic diff below.

## Compiler questions and decisions

**Q1 — which scope is authoritative?** The live V1 frontmatter says
`scope: project`, while its install path is
`projects/agent-queue/playbooks/pr-merge-sweep.md`. The staged source uses
`scope: project:agent-queue`, the unambiguous V2 representation of that install
path. This fixes the inventory `scope_conflict`; it does not broaden runtime
reach.

**Q2 — may the live V1 file be rewritten now?** No. The live vault remains a
V1 runtime input and carries the only graph V1 can execute. The byte-exact
snapshot is `legacy-v1.md`; only `source.md` is prose-only V2 authoring input.
The atomic deployment/switch sequence below defers the live replacement until
V1 admission is closed.

## Semantic diff versus the V1 graph

The deterministic `lower_pipeline` path reads `legacy-v1.md` and produces the
three V2 steps in `artifact.json`. Executable semantics are unchanged:

- `timer.30m` selects `sweep-open-prs` with no guard.
- `ensure_task` preserves `project_id=agent-queue`, dedup key
  `pr-merge-sweep`, title, complete literal description, `profile_id=pr-merger`,
  priority `15`, binding `sweep`, and its success/failure targets.
- `task_route` preserves `sweep.task_id`, `profile_id=pr-merger`, and
  `intelligence_class=deep-medium`; every target still ends at `done`.
- V2 names steps `sweep-open-prs--<V1 node id>` and changes source references
  from JSON lines to prose lines only. These are presentation changes.

The V1 `cooldown: 1500` is not represented in the typed V2 artifact. This is
not observable for this playbook: its only trigger arrives every 1800 seconds,
which already exceeds the cooldown. The staged source records the fact so a
future trigger-cadence edit is reviewed rather than silently changing it.

## Capabilities and why each is needed

| Command | Required behavior |
|---|---|
| `ensure_task` | Creates or reuses the one deduplicated sweep task. |
| `task_route` | Assigns the reused-or-created task to `pr-merger` on `deep-medium`. |

The project scope in the artifact is `project:agent-queue`; it is the V2
authorization boundary. The staged profile snapshot records the exact current
legacy capability policy whose fingerprint is in `compiled_against.profiles`.

## AI profiles, budgets, and output schemas

There are no LLM steps, token budgets, or output schemas. `pr-merger` is a
delegated task profile in both commands; its staged profile fingerprint is
`sha256:82f58c786a1fa017594b31fefa1b14b9dfd8beb5ea30ebc255c165a2f80e2faa`.
The profile's default class remains `deep-medium`, and the explicit
`task_route` argument remains the V1 value.

## Accepted behaviour differences

1. V2 uses namespaced global step ids where V1 scoped node ids to a rule.
2. The staged V2 source is prose-only; `legacy-v1.md` is retained as historical
   lowering/parity evidence until V1 execution is retired.
3. The source scope becomes explicit `project:agent-queue`, matching the vault
   install path instead of the less-specific V1 frontmatter.
4. No approval, activation, or live-vault replacement is part of this record.

## Atomic deployment/switch sequence

1. Keep `projects/agent-queue/playbooks/pr-merge-sweep.md` unchanged while the
   daemon runtime is V1. Import this artifact only through the artifact-import
   command delivered by `solid-harbor.72`; it must verify the source and
   artifact SHA-256 values above and keep the import inactive.
2. A human operator reads this review record and semantic graph, then supplies
   their identity/date and changes the decision to `approved` in the imported
   review evidence. No agent may make that attestation.
3. Follow the cutover runbook: close V1 admission, drain every active V1 run,
   re-check readiness, and obtain the required named cutover authorizations.
4. In the same controlled deployment that enables the approved V2 activation,
   replace the live V1 vault source with this prose source. Do not expose a
   prose-only source to a V1 runtime.
5. Use the normal rollback window. A rollback to V1 restores the retained V1
   source and leaves V2 artifact bytes intact.
