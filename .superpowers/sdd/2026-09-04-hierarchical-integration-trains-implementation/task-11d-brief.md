# Task11d requirements — CLI, doctor, operator guide

Begin only after reviewed11a/11b/11c. Read this first, task-11-brief.md binding
constraints, task-11-slices.md and final11b/11c reports for actual interfaces. Read
src/cli/CLAUDE.md before CLI edits; its old statement that bearer tokens are ignored
is stale descriptive text, not permission to bypass current authenticated transport.
Current server ExecutionPrincipal/RequestScope authority is binding.

1. Add src/cli/integration.py and register in app.py before auto commands. Provide
status,flush,enable,probe,waive-history,resume,abort,retry-cleanup through reviewed
generic command envelope. Use _get_client/_run/emit, lazy heavy imports, existing
error/exit conventions, JSON and brief envelopes. CLI never opens DB or key files,
never manufactures proof/operator identity. Expose11c guarded project repository/
policy configuration through existing project CLI transport as needed. Do not
hand-edit generated clients; generic execute should require no DTO/router change.

2. Extend src/doctor/integration_checks.py with read-only checks using reviewed
status/preflight projections for schema readiness, config/key/transport authority,
effective protection/trust/hosted-variable/probe freshness, cutover/draining and
session-instance-token readiness. No automatic credential, protection, probe or
migration fixes; no real probe as part of doctor. Missing/unverifiable evidence is
visible. Preserve worker-scope operator-DB migration refusal. Read-only provider
checks, where applicable, are fake in tests and never mutate production.

3. Write docs/guides/hierarchical-integration-trains.md with exact actual CLI commands
and safe operator sequence: inspect current DB; operator-only upgrade; restart;
configure valid non-secret absolute key references and both App identities; required
compiled policies/primary/debug/branchless-verifier bindings; hosted trust variables;
observe; verified installed worker/daemon isolation; explicit preprovisioned scratch
probe; hierarchy; train; status/flush; human repair/resume/abort; cleanup retry; disable
to drain; explicit legacy-policy restoration. No DB downgrade/history deletion for
rollback, no automatic feature enablement. Existing frozen operations finish safely.

Document deliberate release limits: GitHub.com only; unsupported/same-UID unconfined
stock deployments stay disabled/observe until isolation is verifiable, no YAML claim
can waive it; ordinary task PRs still full-CI fallback; main promotion is exact already-
tested OID with no post-main audit; all nonempty batches ephemeral including singleton;
successful integration branches deleted, failed forensic retention default604800s;
configuration edits requiring disabled/drained state; integration credentials restart-
required; fresh server-issued session-instance-bound tokens/restarted managed writers;
explicit history waiver changes applicability, not forged review receipts.

4. Focused CLI JSON/brief/error/authorization transport tests, doctor read-only/no-fix
and worker DB refusal tests, guide command-name parity, session-token rollout coverage.
Use fake adapters and scratch test DB only. One final affected-area gate, aq test no
upwardworkers/fullsuite, Ruff changedfiles/diffcheck. If public API DTO/router changes,
regenerate both clients with repo scripts and run contract tests, no manual edits.

Use apply_patch/exact-path commits, preserve unrelated work, no subagents, no push/PR/
main merge/production activation/operator DB/config/probe. Full task-11d-report.md
contains exact RED/GREEN commands/output, checklist/self-review/concerns. Return short
handoff; Task12 still owns final cross-boundary, copy, migration and isolated-daemon
release evidence. Do not call deployment ready merely because CLI tests pass.
