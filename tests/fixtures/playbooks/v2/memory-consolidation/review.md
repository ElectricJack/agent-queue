---
playbook_id: memory-consolidation
artifact_sha256: "sha256:c69b44e5af6bf80fe9344ee7480969acf84c32c6b4e7baca43c16a27970230ff"
source_sha256: "sha256:397d8826c2559f3c083b00ccd044f93a545690d7989410b4d8fe6b1b4139e9e5"
contract_fingerprint: "sha256:90d67fce3cb16821f9b06a366068d89f78230bcecaabe95fa61e4e7f7f187071"
reviewed_by: "aq task agile-impact-36 (worker-deep-high-codex); operator sign-off is this fixture's PR review"
reviewed_at: "2026-09-03"
decision: approved
questions_resolved: 3
capabilities_granted:
  aq_commands: [create_task, list_projects, render_prompt]
  harness_tools: []
  plugin_tools: [count_project_memory_files, read_project_memory_file]
profiles_referenced: [supervisor]
---

# Reviewed V2 artifact — `memory-consolidation`

## Compiler questions and decisions

**Q1 — how should prose-only V1 behavior map to V2?** Decision: preserve the
complete source body as one schema-bound LLM prompt. Target selection and task
creation happen in one durable tool loop; `tasks_created` is validated before
the completed edge.

**Q2 — which profile owns the call?** Decision: `supervisor`, because the
source delegates consolidation scheduling to supervisor authority.

**Q3 — the source named generic `read_file`, but the shipped file plugin has a
project-memory boundary.** Decision: use `read_project_memory_file`, which
confines reads to the selected project's memory directory.

## Semantic diff versus the V1 graph

There is no V1 machine graph. Structural parity preserves the `timer.24h`
trigger, full prose prompt, five tools, output shape, and completed/failed
terminal split. V2 makes the former implicit LLM sequence one budgeted state.

## Capabilities and why each is needed

`list_projects` enumerates candidates; `read_project_memory_file` and
`count_project_memory_files` apply the churn threshold; `render_prompt` builds
the task body; `create_task` creates one task per selected project. No wildcard
or harness capability is granted.

## AI profiles, budgets, and output schemas

The `supervisor` profile supplies `deep-high`. Hard limits are 50 calls, 4,096
output tokens, 65,536 total tokens, and 900 seconds. The schema requires only
`tasks_created[]` objects containing `project_id` and `task_id`.

## Accepted behaviour differences

1. Three prose steps execute within one V2 tool loop instead of three implicit
   V1 turns, avoiding untyped intermediate durable state.
2. The provider resolves through the `supervisor` profile and headless LLM
   configuration instead of the retired source-level Gemini model pins.
