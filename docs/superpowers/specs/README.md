# Feature specs and designs (historical)

<!-- aq:historical -->
> **Historical design records.** Each file describes one feature as it was
> designed, not as the code stands today. Start at
> [the documentation home](../../README.md) for current behaviour.

A design here states the problem, the model chosen, the invariants and the
failure modes for one feature. Several are the authoritative *argument* behind
behaviour that still ships — swarm pools, the task-graph layout engine, the
Discord simplification, hierarchical integration trains — which is why they are
cited from current pages as background.

Where a design and a page under [`docs/concepts/`](../../concepts/) disagree, the
concept page was written against current source and wins.

| Date | Page | File |
|---|---|---|
| 2026-09-08 | Global worker pools | [`2026-09-08-global-worker-pools-design.md`](2026-09-08-global-worker-pools-design.md) |
| 2026-09-08 | Discord simplification implementation spec | [`2026-09-08-discord-simplification-implementation.md`](2026-09-08-discord-simplification-implementation.md) |
| 2026-09-08 | Deleting tasks that own a materialized branch | [`2026-09-08-task-deletion-with-materialized-branches-design.md`](2026-09-08-task-deletion-with-materialized-branches-design.md) |
| 2026-09-07 | SQLite Removal — PostgreSQL as the Only Backend | [`2026-09-07-sqlite-removal-implementation.md`](2026-09-07-sqlite-removal-implementation.md) |
| 2026-09-07 | Recent work and model attribution in the Tasks tab | [`2026-09-07-recent-work-and-model-attribution-design.md`](2026-09-07-recent-work-and-model-attribution-design.md) |
| 2026-09-07 | Provider usage — implementation spec | [`2026-09-07-provider-usage-implementation.md`](2026-09-07-provider-usage-implementation.md) |
| 2026-09-07 | Provider usage in the dashboard | [`2026-09-07-provider-usage-design.md`](2026-09-07-provider-usage-design.md) |
| 2026-09-07 | Project folders and drag/drop in the left rail | [`2026-09-07-project-folders-and-drag-drop-design.md`](2026-09-07-project-folders-and-drag-drop-design.md) |
| 2026-09-06 | Release Roadmap — Tier 1 through Ongoing | [`2026-09-06-release-roadmap.md`](2026-09-06-release-roadmap.md) |
| 2026-09-06 | Blocked-task escalation playbook | [`2026-09-06-blocked-task-escalation-design.md`](2026-09-06-blocked-task-escalation-design.md) |
| 2026-09-06 | Assignment routing as an authored playbook | [`2026-09-06-assignment-routing-as-playbook.md`](2026-09-06-assignment-routing-as-playbook.md) |
| 2026-09-05 | CI main sentinel — keep `main` green without a human in the loop | [`2026-09-05-ci-main-sentinel-design.md`](2026-09-05-ci-main-sentinel-design.md) |
| 2026-09-04 | Review: Hierarchical Delivery and Integration Trains | [`2026-09-04-hierarchical-integration-trains-review.md`](2026-09-04-hierarchical-integration-trains-review.md) |
| 2026-09-04 | Playbook V2 fresh-cutover design | [`2026-09-04-playbook-v2-fresh-cutover-design.md`](2026-09-04-playbook-v2-fresh-cutover-design.md) |
| 2026-09-04 | Hierarchical Delivery and Integration Trains | [`2026-09-04-hierarchical-integration-trains-design.md`](2026-09-04-hierarchical-integration-trains-design.md) |
| 2026-09-04 | Dashboard performance investigation — task system, PostgreSQL, task graph | [`2026-09-04-dashboard-performance-investigation.md`](2026-09-04-dashboard-performance-investigation.md) |
| 2026-09-04 | Dashboard Performance Implementation Plan | [`2026-09-04-dashboard-performance-implementation.md`](2026-09-04-dashboard-performance-implementation.md) |
| 2026-09-03 | Project onboarding from the dashboard | [`2026-09-03-project-onboarding-design.md`](2026-09-03-project-onboarding-design.md) |
| 2026-09-01 | Task graph: server-side stable layout, viewport paging, and focus mode | [`2026-09-01-task-graph-spatial-layout-design.md`](2026-09-01-task-graph-spatial-layout-design.md) |
| 2026-09-01 | Playbook V2 Semantic Graph Design | [`2026-09-01-playbook-v2-semantic-graph-design.md`](2026-09-01-playbook-v2-semantic-graph-design.md) |
| 2026-09-01 | Dashboard vitest flakiness — root cause and fix | [`2026-09-01-dashboard-vitest-flakiness.md`](2026-09-01-dashboard-vitest-flakiness.md) |
| 2026-08-31 | Playbook-owned intelligence routing | [`2026-08-31-playbook-intelligence-routing-design.md`](2026-08-31-playbook-intelligence-routing-design.md) |
| 2026-08-31 | Mandatory triage through a shared playbook | [`2026-08-31-mandatory-triage-playbook-design.md`](2026-08-31-mandatory-triage-playbook-design.md) |
| 2026-08-30 | The LLM direct path — replacing `Supervisor.chat()` and `chat_provider` | [`2026-08-30-llm-direct-path-design.md`](2026-08-30-llm-direct-path-design.md) |
| 2026-08-30 | Projectless global supervisor | [`2026-08-30-projectless-supervisor-design.md`](2026-08-30-projectless-supervisor-design.md) |
| 2026-08-30 | Playbooks in Command Center | [`2026-08-30-command-center-playbooks-proposal.md`](2026-08-30-command-center-playbooks-proposal.md) |
| 2026-08-30 | Global Agent flock | [`2026-08-30-agent-flock-design.md`](2026-08-30-agent-flock-design.md) |
| 2026-08-30 | Command Center task workspace | [`2026-08-30-command-center-unification-design.md`](2026-08-30-command-center-unification-design.md) |
| 2026-08-30 | Agent question routing — approved design | [`2026-08-30-agent-question-routing.md`](2026-08-30-agent-question-routing.md) |
| 2026-08-28 | Swarm work model — hierarchy, claims, pools, formulas | [`2026-08-28-swarm-work-model-design.md`](2026-08-28-swarm-work-model-design.md) |
| 2026-08-27 | Session desired state — design | [`2026-08-27-session-desired-state-design.md`](2026-08-27-session-desired-state-design.md) |
| 2026-08-25 | Live Pane Streaming + Agents Console Grid — Design | [`2026-08-25-live-pane-streaming-design.md`](2026-08-25-live-pane-streaming-design.md) |
| 2026-08-24 | Usage-Aware Concurrency — Track Real Headroom, Spend It Before Reset | [`2026-08-24-usage-aware-concurrency.md`](2026-08-24-usage-aware-concurrency.md) |
| 2026-08-22 | Pane View: `task-detail` — Design | [`2026-08-22-pane-task-detail-design.md`](2026-08-22-pane-task-detail-design.md) |
| 2026-08-22 | Pane View: `spec-doc-reader` — Design | [`2026-08-22-pane-spec-doc-reader-design.md`](2026-08-22-pane-spec-doc-reader-design.md) |
| 2026-08-22 | Pane View: `session-peek` — Design | [`2026-08-22-pane-session-peek-design.md`](2026-08-22-pane-session-peek-design.md) |
| 2026-08-22 | Pane View: `playbook-run-inspector` — Design | [`2026-08-22-pane-playbook-run-inspector-design.md`](2026-08-22-pane-playbook-run-inspector-design.md) |
| 2026-08-22 | Pane View: `file-browser` — Design | [`2026-08-22-pane-file-browser-design.md`](2026-08-22-pane-file-browser-design.md) |
| 2026-08-22 | Pane View: `console-stream` — Design | [`2026-08-22-pane-console-stream-design.md`](2026-08-22-pane-console-stream-design.md) |
| 2026-08-22 | Pane View — `proposal-preview` — Design | [`2026-08-22-pane-proposal-preview-design.md`](2026-08-22-pane-proposal-preview-design.md) |
| 2026-08-22 | Pane View — `diff-review-changes` — Design | [`2026-08-22-pane-diff-review-changes-design.md`](2026-08-22-pane-diff-review-changes-design.md) |
| 2026-08-22 | Pane View — `contextual-settings` — Design | [`2026-08-22-pane-contextual-settings-design.md`](2026-08-22-pane-contextual-settings-design.md) |
| 2026-08-22 | Pane Plugin Interface — Design | [`2026-08-22-pane-plugin-interface-design.md`](2026-08-22-pane-plugin-interface-design.md) |
| 2026-08-22 | Dashboard Shell v2 — Design | [`2026-08-22-dashboard-shell-v2-design.md`](2026-08-22-dashboard-shell-v2-design.md) |
| 2026-08-21 | Dashboard v2 + Work Pipeline Design | [`2026-08-21-dashboard-v2-and-work-pipeline-design.md`](2026-08-21-dashboard-v2-and-work-pipeline-design.md) |
| 2026-05-07 | Agent Reconciliation Design | [`2026-05-07-agent-reconciliation-design.md`](2026-05-07-agent-reconciliation-design.md) |
| 2026-04-27 | Runtime Rename + ACP Adoption — Combined Design | [`2026-04-27-runtime-rename-and-acp-design.md`](2026-04-27-runtime-rename-and-acp-design.md) |
| 2026-04-27 | Email Playbook Sandboxing — Drafts-Only Outbound for Moss & Spade Business Logic | [`2026-04-27-moss-spade-email-sandboxing-design.md`](2026-04-27-moss-spade-email-sandboxing-design.md) |
| 2026-04-25 | Profile Validation + Platform-Driven Dispatch (Phase 2) | [`2026-04-25-profile-validation-design.md`](2026-04-25-profile-validation-design.md) |
| 2026-04-25 | Platforms Implementation (Phase 1) | [`2026-04-25-platforms-implementation-design.md`](2026-04-25-platforms-implementation-design.md) |
| 2026-04-25 | Extracting the Memory Plugin to `aq-memory` (External Plugin) | [`2026-04-25-aq-memory-extraction-design.md`](2026-04-25-aq-memory-extraction-design.md) |

Every file here has a recorded disposition in
[the disposition ledger](../../history/disposition-ledger.md).
