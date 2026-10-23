# Project-defaults building-block port

Review candidate for task `crisp-meadow-72`; not installed or activated. This
operator-local SYSTEM playbook is not added to shipped defaults or startup
requirements. Its only rule is `file-graft-hooks-chore` on `project.created`.

The supervisor supplied the 2026-10-07 `operator-contract-refresh/project-defaults`
bundle and its active artifact in a read-only snapshot. The unchanged recordings,
including the earlier `baseline.json` and `review.json`, are retained under
`tests/fixtures/playbooks/project-defaults/installed/`; their raw byte hashes
are in the adjacent `SHA256SUMS`. The active artifact is
`sha256:b9976e3316b33c30d462160b579ad4e564b1d2d9b10b5322250357b4fce6fdb8`.

The active artifact, baseline and recorded semantic body already include the
MCP template merge and `.mcp.json` commit instruction missing from the installed
source's quoted chore. The candidate source quotes those active instructions
verbatim. This corrects source evidence without changing the command argument.

| Construct | Disposition and reason |
|---|---|
| Typed command result | Retain `ensure_task` and its `created`/`reused` → completed, `rejected`/`runtime_error` → failed edges. No generic dictionary-success decision is needed. |
| Capability metadata | Declare only `ensure_task`; real-handler scenarios preserve creation, reuse and refiling. The declaration is not a grant from an operator. |
| Deduplication | Retain `ensure_task`'s project-scoped live-task key. State/notify would change replay and post-completion refiling and introduce unrelated state/messages. |
| Host checks and repository edits | Retain the worker chore verbatim. A script or doctor precheck before filing would change missing-graft behavior, failure timing and the normal worker delivery boundary. |
| Collections, foreach and observation snapshots | No collections or scans exist. Adding them would add limits, receipts and failure modes without replacing logic. |
| State version and scripts | Omitted: no state namespace or script is used. |

There are no newly handled events. The `workspace_in_vault: false` trigger,
project binding, dedup key, title, class hint, description bytes, result binding,
outcome edges and lack of retry are preserved. Source references and compilation
metadata identify the candidate separately; the original golden bytes are not
rewritten.

Rebuild this candidate only with:

```bash
python docs/playbooks/building-blocks/project-defaults/build.py
python docs/playbooks/building-blocks/project-defaults/build.py --check
```

The local builder invokes the same pure `propose` compiler and registered
contracts/events as the reviewed-bundle authoring tool, with source inventory
enforcement. It does not write the vault or register a shipped bundle.
`semantic-body.json` preserves the active policy; the builder relocates source
references onto the candidate prose. `manifest.md` explicitly records pending
review, and `diagnostics.json` contains compiler diagnostics.

`aq playbook commands --name ensure_task --include-schema` verified the live
typed argument schema, keyed deduplication, `ensure_task` capability and command
availability. `tests/test_project_defaults_playbook.py` checks unchanged snapshot
hashes, active artifact identity, graph/argument equality and reproducible
compilation. Its scenario fixture exercises both artifacts through the real V2
engine, typed adapter and CommandHandler with a disposable PostgreSQL database.
Run storage/activation discovery use test repositories; no installed activation
is queried or changed. Success and replay use real task creation/deduplication.

Both artifacts expose an existing adapter defect on handler refusal and handler
exceptions: the builtin adapter constructs a rejected `EnsureTaskValue` without
its required fields, so executor validation selects `contract_violation` and
fails the run without retry. The scenarios preserve this observed baseline;
they do not change the shared adapter. A separate valid typed-rejection injection
exercises the authored `rejected` edge, while a command-boundary exception
exercises `runtime_error`. These injected failures are distinguished in
`scenarios.json`.

Validation: 21 focused equivalence tests and 431 related tests passed through
`aq test` (ensure_task, command executor, V2 compiler, run authority, default
artifacts, shipped sources and selection catalogue). Changed-file Ruff and the
candidate builder's `--check` passed. Exact commands are recorded on the task.

Operator review
must bind the candidate's exact source/artifact hashes before any deliberate
import or activation; this bundle carries no authorization to perform either.
