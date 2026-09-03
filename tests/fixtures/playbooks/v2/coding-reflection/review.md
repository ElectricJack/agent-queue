---
playbook_id: coding-reflection
artifact_sha256: "sha256:7e973e10f5a3cf8f6fd7f40e4aa52030cd90cb9d3be9326195d70dd297e5f408"
source_sha256: "sha256:dfe80fc7f8c4cce602be7b90704e45e02058f86026995fc19cae217ca02042ce"
contract_fingerprint: "sha256:c2afcdfce14a88082b58792c5fe7585d54e59bd7986100f68b6890d0113de4b2"
reviewed_by: "aq task agile-impact-36 (worker-deep-high-codex); operator sign-off is this fixture's PR review"
reviewed_at: "2026-09-03"
decision: approved
questions_resolved: 3
capabilities_granted:
  aq_commands: [get_task]
  harness_tools: []
  plugin_tools: [git_diff, memory_save, memory_search]
profiles_referenced: [worker-deep-high-claude]
---

# Reviewed V2 artifact — `coding-reflection`

## Compiler questions and decisions

**Q1 — no V1 graph exists.** Decision: preserve the full prose as the prompt
for two explicit rules, one for `task.completed` and one for `task.failed`.

**Q2 — the source declared `agent-type:coding` but installs only under
`claude-opus`.** Decision retained from the first review: correct the scope to
`agent-type:claude-opus`; keep `id: coding-reflection` so installed copies are
not orphaned.

**Q3 — the prose used retired memory verbs.** Decision: use the shipped
`memory_search` and `memory_save` surfaces. Corrections are saved as newer,
explicit insights instead of deleting history through an unavailable command.

## Semantic diff versus the V1 graph

There is no V1 machine graph. V2 records two rules with identical prompts,
profiles, budgets, schemas, and terminal topology. This preserves both triggers
without hiding the event outcome in model prose.

## Capabilities and why each is needed

`get_task` reads the triggering record, `git_diff` reads an associated change,
`memory_search` finds related insights, and `memory_save` persists useful new
or corrected guidance. No harness tools or wildcards are granted.

## AI profiles, budgets, and output schemas

Both rules use `worker-deep-high-claude`. Each is capped at 20 calls, 4,096
output tokens, 32,768 total tokens, and 600 seconds. Output must contain an
integer `insights_saved`, boolean `skipped`, and string `summary`.

## Accepted behaviour differences

1. V2 performs each reflection in one tool loop rather than implicit V1 prose
   sections, with a typed final summary.
2. Retired `memory_store`/`memory_recall`/`memory_delete` names become the
   supported `memory_save`/`memory_search` interface.
