# Playbook V2 Fresh Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate four reviewed Playbook V2 replacements, retire live V1 definitions, switch the fleet to V2, and purge completed task history after validation.

**Architecture:** Use the landed reviewed-artifact importer and content-addressed activation store. Keep the orchestrator paused, drain V1 before the atomic runtime switch, and perform the destructive task cleanup only after V2 health and UI smoke tests pass.

**Tech Stack:** Python 3.12, asyncio, Pydantic, SQLAlchemy, Typer CLI, React/Vite dashboard, SQLite/PostgreSQL adapters.

**Spec:** `docs/superpowers/specs/2026-09-04-playbook-v2-fresh-cutover-design.md`

## Global Constraints

- Work on `main`; the user explicitly declined an isolated worktree.
- Keep the orchestrator paused until all cutover and cleanup verification passes.
- Never fabricate a human review, G1 sign-off, or either G2 authorization.
- Import artifacts through `aq playbook v2-import`; never insert artifact rows directly.
- Delete only tasks whose status is exactly `COMPLETED`.
- Verify a restorable database backup before deleting the first task.
- Preserve V1 runtime code until the existing rollback-window close gate passes.

---

### Task 1: Commit recovery fix and establish green baseline

**Files:**
- Verify: `src/commands/system_commands.py`
- Verify: `tests/test_system_commands.py`
- Verify: `tests/test_playbook_v2_import.py`

**Interfaces:**
- Consumes: commit `4b78029a` and the importer merged in `f06399d3`.
- Produces: a clean `main` containing both changes and baseline test evidence.

- [ ] **Step 1: Apply the verified recovery commit**

```bash
git cherry-pick 4b78029a
```

- [ ] **Step 2: Run targeted Python tests**

```bash
pytest tests/test_system_commands.py tests/test_playbook_v2_import.py -q
```

Expected: all SQLite cases pass; PostgreSQL cases pass when the worker DSN is configured or report explicit skips.

- [ ] **Step 3: Verify generated dashboard client and build**

```bash
npm run generate:ts-client -- --from-file
npm --prefix dashboard run typecheck
npm --prefix dashboard run build
```

Expected: generation, typecheck, and build exit 0; `playbookArtifacts` is exported by `packages/aq-ts-client/src/sdk.gen.ts`.

- [ ] **Step 4: Commit the design and plan**

```bash
git add docs/superpowers/specs/2026-09-04-playbook-v2-fresh-cutover-design.md docs/superpowers/plans/2026-09-04-playbook-v2-fresh-cutover.md
git commit -m "docs(playbooks): plan fresh v2 cutover"
```

### Task 2: Approve and verify the four reviewed V2 bundles

**Files:**
- Modify: `tests/fixtures/playbooks/v2/pr-merge-sweep/review.md`
- Test: `tests/test_pr_merge_sweep_v2_migration.py`
- Test: `tests/test_default_playbook_v2_artifacts.py`

**Interfaces:**
- Consumes: exact reviewed artifacts under `tests/fixtures/playbooks/v2/default-assignment-routing/`, `default-pipeline/`, `memory-consolidation/`, and `pr-merge-sweep/`.
- Produces: four importable bundles whose review metadata matches their immutable bytes.

- [ ] **Step 1: Present the PR sweep graph and exact artifact hash to the operator**

Run:

```bash
aq --json playbook v2-validate --path reviewed-playbooks/pr-merge-sweep/artifact.json
```

Expected: no error or question diagnostics and hash `sha256:8b1c7bec5aee1aa4d864d75e203a581a2f8289cbe6a5847b442c545e515d2525`.

- [ ] **Step 2: Record the supplied human identity and UTC review date**

Set `AQ_REVIEWER_NAME` to the exact human identity supplied at this gate. Change only these frontmatter fields in `review.md`, applying `AQ_REVIEWER_NAME` verbatim rather than writing the variable name:

```yaml
reviewed_by: "the value of AQ_REVIEWER_NAME"
reviewed_at: "2026-09-04"
decision: approved
```

- [ ] **Step 3: Write the failing importer coverage change**

Add `"pr-merge-sweep"` to `PLAYBOOK_IDS` in `tests/test_playbook_v2_import.py`; the test must fail before Step 2 because pending evidence is refused and pass afterward.

- [ ] **Step 4: Run bundle tests**

```bash
pytest tests/test_playbook_v2_import.py tests/test_pr_merge_sweep_v2_migration.py tests/test_default_playbook_v2_artifacts.py -q
```

Expected: all applicable tests pass.

- [ ] **Step 5: Commit reviewed evidence**

```bash
git add tests/fixtures/playbooks/v2/pr-merge-sweep/review.md tests/test_playbook_v2_import.py
git commit -m "chore(playbooks): approve pr merge sweep v2 artifact"
```

### Task 3: Stage, import, and activate V2 artifacts

**Files:**
- Copy to: `/home/jkern/.agent-queue/vault/reviewed-playbooks/{default-assignment-routing,default-pipeline,memory-consolidation,pr-merge-sweep}/`
- Replace at switch: live vault playbook Markdown paths discovered by `aq playbook migration-inventory`.

**Interfaces:**
- Consumes: four approved bundle directories and `playbook_v2_import`.
- Produces: four enabled, `ready` V2 activations pinned to full hashes.

- [ ] **Step 1: Read and record live preflight state**

```bash
aq --json playbook migration-inventory
aq --json playbook v1-drain-status
aq --json playbook activation-health
aq --json playbook cutover-gate-status
```

- [ ] **Step 2: Copy complete bundles beneath `vault_root/reviewed-playbooks`**

Copy `artifact.json`, `artifact.sha256`, `source.md`, `review.md`, and `diagnostics.json` for all four ids. Preserve file bytes.

- [ ] **Step 3: Import each bundle inactive**

```bash
aq --json playbook v2-import --path reviewed-playbooks/default-assignment-routing
aq --json playbook v2-import --path reviewed-playbooks/default-pipeline
aq --json playbook v2-import --path reviewed-playbooks/memory-consolidation
aq --json playbook v2-import --path reviewed-playbooks/pr-merge-sweep
```

Expected: each response has `success: true` and `activated: false`.

- [ ] **Step 4: Close V1 admission and cancel every remaining V1 run**

```bash
aq playbook v1-admission-close --reason "retiring v1 for approved v2 cutover"
for run_id in \
  6be00e73-94c 6eda4bb5-dcc a3f21a17-975 \
  b316e604-304 cc490539-ed1 6cf47695-a42
do
  aq playbook v1-run-cancel --run-id "$run_id" \
    --reason "retiring orphaned v1 run before v2 cutover"
done
aq --json playbook v1-drain-status
```

Expected: `drained: true`.

- [ ] **Step 5: Diff and activate each exact imported hash**

Run `artifact-diff` for each id, inspect executable changes, and then activate the exact reviewed hashes:

```bash
aq --json playbook activate --playbook-id default-assignment-routing \
  --artifact-sha256 sha256:14324858444c042d58d2da74211b4ca6a826419b3398ef9a0d521103f432bbbc \
  --acknowledge-diff sha256:14324858444c042d58d2da74211b4ca6a826419b3398ef9a0d521103f432bbbc
aq --json playbook activate --playbook-id default-pipeline \
  --artifact-sha256 sha256:36f25f93328d04b1fe2fc07b630d4481c0e2bd5bcc573ac26b57011b784f6bdf \
  --acknowledge-diff sha256:36f25f93328d04b1fe2fc07b630d4481c0e2bd5bcc573ac26b57011b784f6bdf
aq --json playbook activate --playbook-id memory-consolidation \
  --artifact-sha256 sha256:c69b44e5af6bf80fe9344ee7480969acf84c32c6b4e7baca43c16a27970230ff \
  --acknowledge-diff sha256:c69b44e5af6bf80fe9344ee7480969acf84c32c6b4e7baca43c16a27970230ff
aq --json playbook activate --playbook-id pr-merge-sweep \
  --artifact-sha256 sha256:8b1c7bec5aee1aa4d864d75e203a581a2f8289cbe6a5847b442c545e515d2525 \
  --acknowledge-diff sha256:8b1c7bec5aee1aa4d864d75e203a581a2f8289cbe6a5847b442c545e515d2525
```

Expected: all four activations are enabled and `ready`.

- [ ] **Step 6: Replace live sources and remove retired definitions**

Replace each live V1 Markdown file with its reviewed `source.md`. Remove the disabled `coding-reflection` source if present. Re-run migration inventory and require zero unresolved enabled entries.

### Task 4: Authorize, switch, and smoke-test V2

**Files:**
- Runtime state: Agent Queue config and append-only cutover audit rows.

**Interfaces:**
- Consumes: clean drain and four ready V2 activations.
- Produces: `playbooks.v2_engine=true` and verified V2 execution.

- [ ] **Step 1: Record G1 using the supplied release-operator name**

Set `AQ_RELEASE_OPERATOR` to the exact human name supplied for the release-operator role.

```bash
aq playbook cutover-drain-signoff --signed-by "$AQ_RELEASE_OPERATOR" --reason "verified zero v1 runs and four ready v2 activations"
```

- [ ] **Step 2: Record two distinct G2 identities supplied by the user**

Set `AQ_CUTOVER_AUTHOR` to the exact author name supplied at the gate. `AQ_RELEASE_OPERATOR` and `AQ_CUTOVER_AUTHOR` must identify different people.

```bash
aq playbook cutover-authorize --role author --signed-by "$AQ_CUTOVER_AUTHOR" --reason "reviewed the four exact v2 artifact hashes"
aq playbook cutover-authorize --role release_operator --signed-by "$AQ_RELEASE_OPERATOR" --reason "independently approved the v2 production switch"
```

- [ ] **Step 3: Switch and restart**

```bash
aq playbook cutover-switch --to v2 --reason "switching to four reviewed v2 playbooks"
aq system restart --reason "load v2 importer, sources, and updater fix"
```

- [ ] **Step 4: Re-pause scheduling immediately after restart and verify**

Require daemon health, orchestrator paused, four ready activations, V2 graph responses for all four ids, dashboard load with zero browser errors, and a successful controlled window rehearsal.

### Task 5: Purge completed task history

**Files:**
- Create: `/home/jkern/.agent-queue/backups/pre-v2-fresh-start-2026-09-04/`
- Create: `/home/jkern/.agent-queue/backups/pre-v2-fresh-start-2026-09-04/completed-task-manifest.json`

**Interfaces:**
- Consumes: healthy V2 runtime and paused orchestrator.
- Produces: zero active or archived `COMPLETED` tasks with a verified backup and deletion manifest.

- [ ] **Step 1: Create and verify a database backup**

Use the configured adapter's supported backup operation. Verify the backup by opening it read-only and counting `tasks`, `archived_tasks`, `playbook_activations`, and `playbook_cutover_events`.

- [ ] **Step 2: Produce the completed-task manifest**

Record exact ids, project ids, titles, parent ids, depths, status, and source table. Abort if any selected active completed task has a descendant whose status is not `COMPLETED`.

