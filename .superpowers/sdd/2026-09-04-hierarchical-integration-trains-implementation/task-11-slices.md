# Task11 sequential implementation slices

Controller plan, derived from read-only Sol preparation. Begin only after Task10c
review passes. Each slice has its own Sol implementer, independent spec/quality
review, focused TDD and one affected-area gate. Full Task11 requirements remain in
task-11-brief.md. Same workspace, disabled feature, no live probe or operator changes.

## 11a — Config, read projections, control persistence

Own config closure/restart/redaction, status/task explain, desired/draining/CAS
project state and append-only cutover/waiver/legacy-applicability persistence.
Use current project integration fields without changing direct/pull_request meaning.
Create integration_control_queries.py, status.py and focused schema migration/tests.
Modify config.py/config_editor.py/system_commands.py/handler.py, project model/query
hydration and both DB mixins as needed. No enable/probe command or real activation yet.
Reject inline/unknown scratch-probe fields without logging values; positive identity
stays github_app, scratch_probe has repository ID/full name and distinct negative App
identity/key reference. Status is one consistent read snapshot with sorted blockers;
task explain consumes the same vocabulary. Security facts unavailable yet are visible
blockers, not assumed passing.

## 11b — Transport, protection, scratch probe

Own shared installed transport inspector/runtime fingerprint enforcement, typed
GitHub.com protection/identity/hosted-variable facts, durable replayable explicit
scratch probe and its persistence migration. Use fake external adapters only.
Files askpass_broker.py/manager.py/github_app.py, new protection.py/probe.py,
attestation.py and relevant focused tests. No invented worker confinement; the
server observes launcher authority and unsupported/unproven postures fail closed.
Probe append-only receipt binds all configured identities/digests/versions and both
negative rejection and positive exact-main success; preserve irreversible ambiguity
and exact candidate-ref cleanup, retaining the scratch commit.

## 11c — Controls and atomic project cutover

Own controls.py, command contracts/handlers and exact pre-forge pr_merge guard,
per-project legacy route suppression, full preflight/CAS/waiver/draining and
local-operator resume/abort/retry controls. No global pipeline disable or fabricated
review receipt. Canonical integration commands resolve relationships server-side.
Status same-project, flush authorized with disabled/observe/hierarchy read-only and
train durable schedule request. Human controls LOCAL only. Include unresolved Git,
attestation AND cleanup irreversible prewrites in safe draining/reconciliation;
expiry is not proof a provider write did not happen. Abort never rewinds main.

## 11d — CLI, doctor, guide and phase integration

Own cli/integration.py/app registration (read CLI instructions), read-only doctor
checks, operator guide and command transport tests. Generic execution/emit only,
no local CLI DB/key access. Guide upgrade/restart/observe/probe/hierarchy/train,
fresh session-instance tokens, controls and draining rollback. Ordinary task PR
full-CI fallback explicitly documented. No actual activation or deployment.

## Migration ownership

Each sequential schema-bearing slice owns a focused new revision and dual-dialect
tests, with downstream code consuming reviewed tables. This replaces the preliminary
single-phase-migration choice: probe state must be designed with its protocol, not
speculatively frozen before transport/probe implementation. No parallel DDL writers.

## Worker-authority preflight decision

No operator-authored confinement booleans or writable-root assertions are proof.
Server producer enumerates all routable profiles/harnesses/workspace variants, not
only live sessions, through current launcher composition/resolution. Observe actual
UID, effective permission/bypass posture and executable/path pins; do not expose raw
argv/environment/credential bytes in status or persisted receipts. Fingerprint safe
canonical facts and hash private details where needed. Worktrees/cgroups/harness
approval flags alone do not establish filesystem isolation.

Default current tmux/subprocess same-UID posture is unconfined and fails closed with
askpass_worker_writable/worker_confinement_unverifiable. A fakeable typed inspector
may report verified external confinement only from actual pinned launcher/kernel
policy observations, not YAML claims. Task11 does not create a new sandbox architecture.
No worker-authority config additions are needed in11a.11b owns the server facts model,
producer/inspector and tests;11d must plainly document stock deployment limitation.
