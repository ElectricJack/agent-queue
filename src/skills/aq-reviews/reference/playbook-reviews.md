# Playbook reviews

Reference for the aq-reviews skill ([../SKILL.md](../SKILL.md)): submitting a
review that asks approval for a Playbook V2 policy, and reviewing one.

## Contents

- Submitting a playbook review (author)
- Review commands in playbooks (reviewer and author)

## Submitting a playbook review

A review that asks approval for a Playbook V2 policy is kind `other` and names
the playbook, so that approval can store the exact artifact it approved:

1. Write the playbook's Markdown source into the vault first
   (`projects/<pid>/playbooks/<id>.md` or `system/playbooks/<id>.md`), and
   draft its semantic body — JSON with exactly `rules` and `steps` — iterating
   with `aq playbook v2-propose` until it is activatable.
2. Submit the document with the playbook attached:

   ```bash
   aq review submit --task-id <task> --file <draft> --kind other --title "Playbook V2: <id> (<what it does>)" --playbook-id <id> --playbook-body <body.json> [--activate-on-approval]
   ```

   The daemon compiles the vault source with that body and refuses the
   submission (`playbook_invalid`, with the diagnostics) unless the artifact is
   activatable. The result's `playbook.artifact_sha256` is the exact hash
   approval will store; put it in your close summary.
3. A revision recompiles: resubmit with `--review-id` and a new
   `--playbook-body`, or omit it to reuse the previous rules and steps against
   the current source.

Nothing is imported or activated before approval. Approval stores the
artifact and sends the supervisor the `aq playbook activate` command, or
activates it when you passed `--activate-on-approval`.

## Review commands in playbooks

Catalogue coverage and caller authority are independent. `review_dispatch`,
`review_show` and `review_decide` are discoverable through generated contracts;
coverage does not turn a PLAYBOOK principal into a supervisor or human decider.
Consult `playbook_commands` for schemas/outcomes and the current caller gates.
Review decisions still require current authorized human/delegated evidence.
An ordinary `agent_task` reviewer is outside the dispatch ledger and its comments
are refused `not_dispatched`. Unregistered event types fail `unknown_event`;
never infer an event schema from a command name. The four universally denied
commands are `run_command`, `shutdown`, `restart_daemon`, `update_and_restart`.

For a playbook proposal, review the actual paths, predicates, thresholds,
recipients, intervals, completeness gates and state revisions against source.
Required capabilities are declarations, never grants. Source supplies state
version and script manifests/policy; server code resolves digests and execution
identity. Compiler output contains exactly `rules` and `steps`, with the existing
source-checked integration-policy compatibility path. A state version change
selects a new namespace and can re-alert; reset is an explicit operator decision.
Snapshot absence is valid only after complete producer coverage and all pages.
Scripts use ordinary processes, timeout/output caps and strict optional JSON;
there is no sandbox prerequisite or real-host gate. Ambiguous execution cannot
be automatically replayed. Protected streams require run-authorized job readback.
See `docs/guides/playbook-building-blocks.md` for rollout and rollback evidence.
