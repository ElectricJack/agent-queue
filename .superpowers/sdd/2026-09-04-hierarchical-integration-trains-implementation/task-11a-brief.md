# Task11a requirements — config/status/control persistence

Read this first, then task-11-brief.md for binding global constraints and policy,
task-11-slices.md for the exact split and worker-authority decision, and
task-11-recovery-interface-map.md for current entrypoints. This is ONLY11a;
11b transport/probe,11c controls/cutover and11d CLI/doctor/guide are later reviews.
Do not implement their runtime behavior early or skip11a evidence because a later
slice will use a seam. Existing feature remains disabled; no external mutations.

## Deliverables

1. Preserve projects.integration_mode direct/pull_request and the existing effective
hierarchical_integration_mode. Add separate desired mode, draining and CAS generation
using typed project model/query hydration. Create focused control persistence for
append-only cutover transitions, explicit operator history waivers and their immutable
consumption/applicability, plus reversible per-project legacy suppression state. Store
full old/new legacy policy snapshot needed for later forward rollback, operator identity,
reason and exact blocker digest. Waiver use never changes a pinned-review receipt or
resolves/deletes historical gates. No effective-mode changes are executed in11a.

2. Own one new focused migration after the then-reviewed head, tables.py and both
src/database/adapters/{sqlite,postgresql}.py mixins. New
src/database/queries/integration_control_queries.py supplies conn-owned writes so11c
can perform one hierarchy-locked cutover CAS. Immutable history UPDATE/DELETE refused
on both backends; all constraints explicitly named. Downgrade must refuse live drain/
transition state before any destructive DDL, never silently lose recovery evidence.
Probe tables belong to11b, not speculative schema here. Document public query/model
interfaces in the report for downstream implementers.

3. Add closed non-secret integration.scratch_probe configuration: exact scratch
repository numeric ID/full name and distinct negative App/install/client identity with
private-key path reference, reusing integration.github_app as positive identity. Do
not duplicate positive identity or accept inline secrets. Require positive IDs, exact
owner/repo, negative App distinct from positive. Reject unknown/inline key,PEM,token,
body,auth fields before substitution and at load/editor/schema/get_config boundaries;
errors/logs must never echo input keys/values that could contain secret bytes.
No worker-authority YAML assertions. Whole integration section is restart-required;
reload must not swap cached credential-bearing integration state.

4. Fix raw nested update_config.data logging in commands/handler.py before validation:
only safe section/dry-run plus redaction marker, never _preview on nested payload.
Verify rejected config, exceptions and response serialization do not leak sentinels.

5. Create IntegrationStatusService in src/integration/status.py with status(project_id)
and task_blockers(task_id), using one consistent DB read snapshot and no provider I/O
or mutations. Return effective/desired/draining mode, schedule, active batch/revision/
members, parent readiness, ownership, repair budgets/deadlines/human hold, CI/evidence,
promotion/reconciliation and normalized cleanup/release state. Stable sorted blockers:
open_child,missing_receipt,stale_head,stale_generation,stale_review,
repository_not_designated,active_owner,pending_ci,budget_exhausted,human_hold,
cleanup_conflict. Carry persisted security/probe blockers where available; absence
of later preflight evidence must not imply ready. No credential material or raw argv.
Project/task relationship validation belongs to service as well as later commands.

6. Append shared integration reasons to existing task explain in task_commands.py/
explain.py without replacing ordinary explanations or broadening project read authority.
Keep status implementation separate from later generic command registration;11c owns
that transport and complete authorization matrix. Do not fake readiness services.

## Verification

TDD focused config closure/reload/redaction tests; new tests/test_integration_controls.py
for real DB projections and no-mutation checks; explain regression; new
tests/test_migration_integration_controls.py SQLite+PostgreSQL upgrade/downgrade/live
guard tests on unique scratch databases. Use existing test naming/layout; only run
specific changed files/nodes while iterating, then one explicit affected-area gate.
aq test beyond one file; no upward workers/full repo suite. Ruff changed Python only.
Controller can run exact PG nodes if inherited agent permissions block loopback.

Use apply_patch, preserve unrelated edits, no subagents, no push/PR/merge/main change,
no operator DB/config or daemon startup. Report full RED/GREEN commands/output,
deliverable reconciliation, interfaces and concerns to task-11a-report.md. Commit only
owned paths if allowed; otherwise controller performs exact-path commit. Runtime code
must be reviewed before11b starts.
