# CLI consistency epic — created

Epic: **keen-harbor** — Make the aq CLI consistent, robust, and fully verifiable.

Created through the repaired live `aq task create --graph` path after restarting AQ. Verified 15 children and 24 blocking dependency links. Saved proposal `prop-7d20e020498f` was discarded after successful creation to prevent duplicate tasks.

| Task | Title |
|---|---|
| `keen-harbor.1` | Preserve task-create flags and validate interactive/noninteractive input |
| `keen-harbor.2` | Make task creation return a parseable success response in JSON mode |
| `keen-harbor.3` | Unify success/error envelopes, brief output, and exit codes across the CLI |
| `keen-harbor.4` | Remove database initialization from CLI import and help discovery |
| `keen-harbor.5` | Make global output flags consistent across command positions and help |
| `keen-harbor.6` | Expose task workspace requirements and guard handwritten CLI/backend parity |
| `keen-harbor.7` | Resolve the advertised but unimplemented task ask-human command |
| `keen-harbor.8` | Retire or explicitly deprecate the obsolete plugin hook-log command |
| `keen-harbor.9` | Refresh aq skills, command examples, auth/module docs and profile guidance |
| `keen-harbor.10` | Add a maintained command inventory and CLI conformance regression suite |
| `keen-harbor.11` | Make CLI test preflight and scratch database isolation failures actionable |
| `keen-harbor.12` | Cover stateful CLI workflows with disposable end-to-end smoke tests |
| `keen-harbor.13` | Re-audit CLI consistency and publish final per-command acceptance report |
| `keen-harbor.14` | Fix task graph creation rejected by atomic hierarchy integration guard |
| `keen-harbor.15` | Bootstrap new root task origins from an existing repository ref |

## Live creation repair

New roots bootstrap their exact base from the existing repository default branch. Existing-root adoption still uses the bound branch. Graph creation now files roots and siblings through the atomic integration service, preserving IDs across dependencies, context, acceptance criteria, labels and formula provenance. Hierarchy guards and operator schema were not changed.

Validation: 202 affected tests passed, including ten new regressions; Ruff and git diff checks passed. The isolated swarm smoke run passed 7/8 scenarios. S7 failed waiting 60 seconds for two fresh pool sessions; the disposable daemon was stopped by the harness. This timeout is recorded on the smoke-coverage child. The restarted live daemon returned healthy, and task creation/readback succeeded.

Code changes remain in the shared checkout; no Git commit or PR was created in this request.

```bash
aq task get-tree --task-id keen-harbor
```
