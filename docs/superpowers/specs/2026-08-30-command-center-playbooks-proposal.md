# Playbooks in Command Center

Status: proposal only. This audit changes navigation, not playbook execution or authoring behavior.

## Recommendation

Promote Playbooks to a primary Command Center view alongside Graph and Tasks, available with All projects or a selected sidebar project. Use one implementation and the same URL-based project selection. The old Settings Playbooks page should become a redirect, not another library.

Within Playbooks, provide **Library** and **Runs** views. Keep search, status filters, selected definition/run, and the originating project in the URL so links can be shared and Back restores the view.

- **Library:** searchable definitions with scope, trigger/schedule, enabled state, active version, last result, and running count. A selected project shows its definitions and applicable shared definitions, clearly grouped. New playbook can start from a template, a copy, or Markdown.
- **Runs:** Running, Needs attention, and History filters; show the playbook, actual project, trigger, current node, elapsed time, token use, and outcome. Clicking a row opens the existing run inspector. A full run URL should preserve the exact run, not open only its definition.
- **Definition editor:** Source, Graph, Tests, and Runs. Keep Markdown as the editable source, with a rendered graph and compile diagnostics beside it. Offer explicit draft, test, and publish stages once the backend supports them. Avoid adding a visual graph editor in the first slice.
- **Run inspector:** live node progress, trace, input/output, error/wait reason, and links to the definition, related tasks, and assigned agents. Make approvals and human-input waits prominent. Keep definition enable/disable separate from run controls.

## Existing capabilities and gaps

| Capability | Current state | Dashboard implication |
| --- | --- | --- |
| Create/edit | Existing create form and Markdown editor with source-hash conflict detection | Reuse these components; add accessible entry points in Command Center |
| Compile | Successful Save & Compile immediately replaces the active compiled version; failure retains the prior version | Do not call this a draft save or imply it is unpublished |
| Graph | Backend can return structured nodes/edges and overlays | Wire this API to a read-only graph; replace the current metadata-only Compiled tab |
| Runs | List/inspect APIs and a run-inspector pane exist, including live refresh | Make run rows and running counts clickable; show Running and Needs attention across definitions |
| Dry run | Simulates the active compiled graph without LLM calls, DB writes, or events | Expose a sample-event editor and node trace, explicitly labeled structural simulation |
| Real run | Manual execution uses real providers, persistence, and possible node side effects | Separate Run now from Dry run; show project, version, payload, and limits before starting |
| Project filtering | Definition filtering includes applicable shared playbooks; run summaries lack first-class project/task/agent fields | Add run context and server-side project filtering; never present all runs of a shared definition as belonging to the selected project |
| Pause/cancel | Disabling a definition prevents new starts but does not stop existing runs. Cancellation currently changes the DB record without stopping the runner | Do not present the existing cancel API as a reliable Stop control; add cooperative cancellation |
| Versions/drafts | Runs store version numbers, but there is no complete source/compiled history or unpublished draft lifecycle | Draft testing, publish, rollback, and reliable historical graph display need backend work |

Dry-run limits matter: natural-language branches take the first candidate, and human-input nodes do not pause. It checks structure and a simulated path, not production behavior. It also tests an already active compiled version, so safe testing of unpublished edits needs a separate compile-preview/draft API. A true isolated execution test needs additional runtime isolation and explicit limits.

## First implementation slice

1. Unify the library under Command Center and the sidebar project scope; show inherited definitions alongside local definitions.
2. Add Runs and Needs attention, connect rows to the existing inspector, and add stable run URLs. Carry actual run project context through the API before claiming project-scoped run filtering.
3. Expose the existing graph and structural dry-run APIs. Keep current Save & Compile behavior clearly labeled as activation.
4. Follow with unpublished drafts, preview compilation, explicit publication, version history, and cooperative cancellation. Add task/agent correlation and isolated execution tests as backend-supported features.

## Evidence reviewed

- Browser audit: all 32 project views and four Settings sections, plus task/session/file/playbook details and their cross-view links. Playbook run rows currently do not open the existing inspector.
- `dashboard/src/pages/PlaybookDetail.tsx`: Source/Compiled/Runs UI and current inert run rows.
- `dashboard/src/panes/playbook-run-inspector/index.tsx`: existing inspector, resume/cancel controls, and live refresh.
- `dashboard/src/api/hooks.ts`: playbook list, source, run inspection, and mutations.
- `src/commands/playbook_commands.py`: definition filtering, run list/inspect, manual execution, dry runs, graph data, source updates, and cancellation limitation.
- `src/api/models/playbook.py`: run summaries and inspection response fields.
- `src/playbooks/manager.py`: compile-and-activate behavior and retention of the prior version after compile failure.
