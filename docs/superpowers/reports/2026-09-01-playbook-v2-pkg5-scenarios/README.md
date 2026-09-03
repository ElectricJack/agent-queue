# Package 5 — §13.3 manual scenario review

Evidence for [`docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md`
§13.3](../../plans/2026-09-01-playbook-v2-graph-api-ui.md) — the seven
screenshots milestone **M5 — Operator legible** requires.

Captured 2026-09-03 against `cc7ba7e2`.

## The gate that used to block this

§13.3 carried a standing "not capturable yet" note: `graph_projection.py`,
`artifact_diff.py` and `run_overlay.py` were absent, and every V2 command
returned `_v2_storage_unavailable`. That is no longer true — all three modules
exist, `_v2_storage_unavailable` is now conditional on
`playbooks.v2_storage_enabled` (`src/commands/playbook_v2_commands.py:460`), and
`dashboard/src/pages/playbook-graph-v2/` renders. The one-line gate the note
prescribes (`ls src/playbooks/graph_projection.py`) passes.

## How these were captured

A worker slot may not run migrations or start a daemon against the operator
database, so nothing here was captured from a running daemon. Instead:

1. `scripts/pkg5_scenarios/build_payloads.py` calls the **real** projections —
   `project_graph`, `diff_artifacts`, `project_overlay`, `evaluate_health` —
   against the **real** §10 fixtures (`tests/fixtures/playbooks/v2/`
   `review-pipeline.artifact.json` v5, `review-pipeline.v6.artifact.json` v6,
   `review-pipeline.receipts.json`) and the **live** command registry
   (`RegistryContractLookup`), and writes one JSON file per scenario holding the
   exact API responses the daemon's V2 command mixin would serve. The fixtures'
   recorded contract fingerprints match the live registry's, so v5 projects as
   `health: ready` without any fixing-up.
2. `dashboard/scenarios/` mounts the production `PlaybookSemanticReview` — the
   same components, hooks and generated client the dashboard tab uses —
   replacing only the fetch transport, which answers from those files. The graph
   response is keyed by artifact hash *and* event scope and the diff by target
   hash, so the artifact chooser and the event-scope filter behave as they do
   against a daemon rather than looking inert.
3. `scripts/pkg5_scenarios/capture.sh` drives the page and writes these files.

Repeat with:

```bash
npm install
./scripts/regenerate-ts-client.sh --from-file
python scripts/pkg5_scenarios/build_payloads.py
(cd dashboard && npx vite --host 127.0.0.1 --port 5199 &)
scripts/pkg5_scenarios/capture.sh
```

**What this evidence does and does not cover.** Every pixel is production UI
rendering production projection output of a real artifact, so it answers each
exit-gate clause about what the graph *shows*. It does not exercise the daemon's
storage, activation-write or pending-event paths; those are covered by
`tests/test_api_playbook_v2_commands.py` and
`tests/test_playbook_activation_commands.py`, not by a screenshot.

## The seven scenarios

| # | §13.3 asks for | File | What it shows |
|---|---|---|---|
| 1 | Branching — `classify-risk` with its seven labelled outgoing edges, `low`/`high` visually distinct from the five reserved ones | [`01-branching.png`](01-branching.png) | Event scope narrowed to `task.completed`. `classify-risk` carries `Low`, `High`, `Invalid Output`, `Budget Exceeded`, `Provider Error`, `Timed Out`, `Cancelled` and `Runtime Error`; `Low` and `High` are solid green `success` edges, the reserved ones dotted red `runtime error` / dashed `timeout` / dash-dot `cancelled`, per the legend. |
| 2 | Convergence — `classify-risk:low` and `escalate:completed` both entering `await-approval`, both labels legible | [`02-convergence.png`](02-convergence.png) | Zoomed, with the `low` edge selected so it is raised above the crowded label band. `Low` (from `classify-risk`) and `Completed` (from `escalate`) both terminate on `await-approval`. |
| 3 | Loop — `for-each-task` with its body, `loop_back` from `check-gate`, and the traversal count from an overlay | [`03-loop.png`](03-loop.png) | `spec.approved` scope with `run-7` overlaid. `Each item` is the `loop body` edge into `open-gate`; `check-gate`'s `case` and `default` edges return to `for-each-task`; `open-gate` reports `5 visits, 5 iterations` and the `Created ×4` edge count. |
| 4 | AI node — `classify-risk` selected, inspector showing `profile_id`, capability namespaces, capability fingerprint, budget and output schema | [`04-ai-node.png`](04-ai-node.png) | Inspector with Advanced expanded: profile `reviewer`, tool use `disabled`, capability fingerprint `sha256:e6a5745b…`, capabilities (`harness tools` none, `aq commands demo_command`, `plugin tools` none), budget (2 calls / 1024 output / 8000 total / 120s) and the `risk: low\|high` output schema. |
| 5 | Invalid node — an artifact whose `gate_create` contract fingerprint was bumped: `stale_contract` diagnostics visible, graph still fully drawn | [`05-stale-contract.png`](05-stale-contract.png) | Header health badge `stale_contract`; Activation reads `Health: stale contract` with the `Command contract changed` reason; the diff panel carries the blocker `Command contract changed for 'gate_create'`. All 13 nodes and both rule clusters still draw. |
| 6 | Diff review — v5 → v6, executable and presentation-only changes separated, activate disabled until acknowledged | [`06-diff-review.png`](06-diff-review.png) | v6 chosen in the artifact chooser against active v5. "3 semantic and 1 presentation changes": executable `check-gate/cases/1` and `ensure-review-task/inputs/title/parts/0/value`; presentation-only `classify-risk/title`. "I reviewed the executable diff" is unchecked and **Activate displayed artifact** is disabled. |
| 7 | Run overlay — a completed run of the v5 artifact while v6 is active: the "older artifact" banner plus the traversed path | [`07-run-overlay-old-artifact.png`](07-run-overlay-old-artifact.png) | v6 active, `run-7` selected. The canvas badge reads `artifact older than the active one`; the overlay panel banner reads "This run used an older artifact: sha256:c2f96f3fa308…"; traversed edges are drawn solid against untraversed grey, and the five loop iterations are listed individually. |

## Observation filed separately

Scenario 5's **Activate displayed artifact** button is enabled even though the
diff reports `activation_blocked` with a `stale_contract` blocker. The server
refuses the activation (`test_activate_requires_acknowledge_diff_for_executable_change`
and the activation command's own checks), so this is a UI affordance gap, not a
correctness hole — `ActivationPanel` gates only on `executableChange` and never
reads `diff.activation_blocked`. Filed as emergent work rather than fixed here.
