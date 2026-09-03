---
playbook_id: coding-reflection
artifact_sha256: null
source_sha256: "sha256:644c4be9799c4fa6362a3b769babcb7af563aaa466192fc4cb366907f9956102"
contract_fingerprint: null
reviewed_by: "aq task solid-harbor.52 (worker-standard-high-claude); operator sign-off is this fixture's PR review"
reviewed_at: "2026-09-03"
decision: rejected
questions_resolved: 2
blocked_on:
  - "requires_agent_proposal: prose LLM playbook with no V1 machine graph to lower"
capabilities_granted:
  aq_commands: []
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Reviewed V2 proposal — `coding-reflection` (not approved)

`decision: rejected` is the locked vocabulary's "recorded, not activatable"
(child plan §3.4). There is no `artifact.json` here because no compile produced
one.

This is the **fourth** shipped playbook the roadmap's file list omits (child
plan §1.2). It lives at
`src/prompts/default_agent_type_playbooks/claude-opus/reflection.md`, is
installed on every startup by `src/vault.py::ensure_default_agent_type_playbooks`,
and is pinned by `tests/test_default_agent_type_playbooks.py`. Its frontmatter id
is `coding-reflection`, which is why this directory is not named `reflection`.

## Compiler questions and decisions

**Q1 — no V1 machine graph, so nothing to lower.** Identical to
`memory-consolidation`: the source is prose, `shadow_compile` reports
`requires_agent_proposal`, and a body synthesised by hand would be a recording of
nothing rather than an approved compile. **Decision: record the question; do not
invent a body.**

**Q2 — the frontmatter scope named a retired agent type.** The file declared
`scope: agent-type:coding` while only ever installing under
`vault/agent-types/claude-opus/playbooks/`; `tests/test_default_agent_type_playbooks.py`
asserts there is no `coding/` directory at all, and
`src/playbooks/handler.derive_playbook_scope` takes the scope from the install
path, so V1 papered over the mismatch. V2's `propose()` reads the frontmatter
instead, which would have produced an artifact scoped to an agent type that does
not exist. **Decision: correct the frontmatter to `scope: agent-type:claude-opus`**
(child plan §5.2 T-6). The id stays `coding-reflection` — renaming it, or
renaming the directory to `coding/`, would orphan every already-installed vault
copy, and `ensure_default_agent_type_playbooks` never overwrites one.

## Semantic diff versus the V1 graph

Not applicable: there is no V1 graph, for the same reason as
`memory-consolidation`. Child plan §4.5 limits LLM playbooks to structural
parity for exactly this case.

## Capabilities and why each is needed

None granted. A future approved artifact must justify each memory-plugin tool
the prose asks for, and a reviewer should confirm the reflection step cannot
reach a task-mutating command — a reflection that can reopen the task it is
reflecting on is a loop.

## AI profiles, budgets, and output schemas

Unresolved along with the body. The source pins `cooldown: 30` and triggers on
both `task.completed` and `task.failed`; V2 expresses cooldown as an activation
property rather than a source field, so mapping it is part of the compile a
human must review.

## Accepted behaviour differences

None accepted, because nothing is activated. The scope correction in Q2 is a
change to the *source*, already made, and is not a difference between V1 and V2
behaviour: on every install path both runtimes resolve this playbook to the
`claude-opus` agent type.
