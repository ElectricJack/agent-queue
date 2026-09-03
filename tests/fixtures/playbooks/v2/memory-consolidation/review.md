---
playbook_id: memory-consolidation
artifact_sha256: null
source_sha256: "sha256:73f587dd70a16aa4efaec20119b2ef8207c39853faf3191fe0f1e285916d3cb3"
contract_fingerprint: null
reviewed_by: "aq task solid-harbor.52 (worker-standard-high-claude); operator sign-off is this fixture's PR review"
reviewed_at: "2026-09-03"
decision: rejected
questions_resolved: 1
blocked_on:
  - "requires_agent_proposal: prose LLM playbook with no V1 machine graph to lower"
capabilities_granted:
  aq_commands: []
  harness_tools: []
  plugin_tools: []
profiles_referenced: []
---

# Reviewed V2 proposal — `memory-consolidation` (not approved)

`decision: rejected` is the locked vocabulary's "recorded, not activatable"
(child plan §3.4), not a judgement on the playbook. There is no `artifact.json`
here because no compile produced one.

## Compiler questions and decisions

**Q1 — this playbook has no V1 machine graph, so there is nothing to lower.**
`src/playbooks/pipeline_lowering.shadow_compile` classifies it by frontmatter
`kind` (absent), reports `requires_agent_proposal`, and stops. The two ```json
fences in the source are output-shape examples (`{"targets": [...]}` and
`{"tasks_created": [...]}`), not an action graph — `is_action_block` in
`tests/test_shipped_playbook_sources.py` is pinned against exactly these two so
that stays true.

**Decision: do not hand-author a semantic body and record it as an approved
compile.** Child plan §5.3 T-8 is explicit that compilation here is LLM-driven
and that the fixture is "the approved recording"; a body synthesised by the
worker that wrote this file would be a recording of nothing. The honest artifact
is this record plus `diagnostics.json`. Producing the real one needs a compiler
agent run whose semantic diff a human reads — the procedure in T-8, which needs
the compiler-agent surface and an operator, not a test fixture.

## Semantic diff versus the V1 graph

Not applicable: there is no V1 graph. V1 executed this source by handing its
prose to an LLM step by step, so its behaviour was never expressed as a graph
and there is no baseline for a structural diff. Child plan §4.5 anticipates
this and limits LLM playbooks to *structural* parity.

## Capabilities and why each is needed

None granted. For the record, the prose instructs the model to call
`list_projects`, `read_file`, `count_project_memory_files`, `create_task` and
`render_prompt`; a future approved artifact must list exactly those and no more,
and the reviewer should check that `create_task` is still called without
`agent_type` (the source explains why: an `agent_type` filter leaves the created
task permanently READY).

## AI profiles, budgets, and output schemas

Unresolved along with the body. The source pins `llm_config.provider: gemini`
with `gemini-2.5-pro`, and `transition_llm_config` with `gemini-2.5-flash` —
a V2 artifact must map both onto profiles and budgets, and the split between the
step model and the transition model is the part a reviewer should look at
hardest, because V2 expresses transition evaluation differently from V1.

## Accepted behaviour differences

None accepted, because nothing is activated.
