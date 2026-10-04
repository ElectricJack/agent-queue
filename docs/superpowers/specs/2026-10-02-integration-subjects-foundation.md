# Integration subjects: schema and shared primitive contracts (phase 1 foundation)

Task `vivid-willow.1` (`p1_foundation`), implementing review `rev-agile-ridge`
revision 2 (content SHA-256 `5a3ef472…fb928`), §3.2–§3.6 and §6 phase 1. This
note records what the foundation ships and the decisions the other phase-1
tasks build on: the observer (`vivid-willow.2`), the reconciler loop
(`vivid-willow.3`), the policy compiler (`vivid-willow.4`) and the root
adapters (`vivid-willow.5`), plus the phase-2/3 owners of `writers.py`,
`records.py`/`gates.py`, `gitops.py` and the CI adapters.

Nothing here is active. Every subject defaults to the `legacy` engine, no code
path creates subjects yet, and no command, CLI, API or playbook surface changed
(`scripts/regenerate-generated.sh --check` is clean).

## What ships

| Piece | Where |
|---|---|
| Revision `a00000000057` (additive; revises `a00000000054`) | `migrations/versions/a00000000057_integration_subjects.py` |
| `integration_subjects`, `integration_subject_journal` | `src/database/tables.py`, documented in `docs/specs/database.md` |
| `IntegrationSubjectQueriesMixin` on the PostgreSQL adapter | `src/database/queries/integration_subject_queries.py` |
| Artifact collection keeps subject and journal pins | `src/database/queries/playbook_artifact_queries.py` |
| Typed ports | `src/integration/subjects.py` |
| Tests | `tests/test_integration_subjects.py`, `tests/test_migration_integration_subjects.py` |

The revision id skips 55 and 56 on purpose: two unmerged branches
(`azure-vault-92.*`, `calm-grove-25.*`) already claim `a00000000055`. Whichever
lands second re-chains onto the other as the migration rules require.

## Decisions

**Waiting and held are not phases.** §3.2 lists `waiting(reason, until)` and
`held(gate)` among the phases. A subject that waits must still know what it
resumes (a `testing` batch waiting for CI), so `phase` keeps the work phase and
`SubjectSchedule` carries the overlay: `wait_reason` + `next_due_at`
(waiting), `gate_id` (held), neither (progressing), `closed_reason` (done).
`Subject.state` derives `progressing | waiting | held | done`.

**The never-blocked rule is a CHECK.** `ck_integration_subjects_never_blocked`:
a `done` subject has a `closed_reason` and no due time, wait or gate; any other
subject has a `gate_id` or a `next_due_at <= due_set_at + max_wait_seconds`.
`max_wait_seconds` is pinned per subject at creation (`DEFAULT_MAX_WAIT_SECONDS`
= 3600, §3.6). `SubjectSchedule`'s validator mirrors the CHECK, and its
constructors (`progress`, `backoff`, `wait`, `hold`, `close`) are the only
legal visit results; `wait` clamps an over-long until to the bound rather than
refusing it (the policy compiler is where an unbounded wait is rejected).

**Events never invalidate a visit.** A visit writes back with
`update_integration_subject_on(expected_version=…)`, a compare-and-swap on
`version`. `wake_integration_subjects` (by subject, task, writer task, batch or
gate) only pulls `next_due_at` forward and stamps `wake_requested_at`; it never
bumps `version`. A visit that passes `visit_started_at` and finds a wake at or
after it keeps the subject due now. A lost event costs latency, never progress.

**Identity and policy are pinned by trigger.** `integration_subject_identity_pinned`
refuses changing id, project, repository, kind, key, task, the pinned policy
playbook/artifact, or a bound `batch_id`; a decreasing `version` or
`generation`; and reopening a `done` subject. Activating a new policy therefore
cannot touch a running subject (p1_policy's acceptance relies on this).

**One journal, append-only and replay-safe.** `integration_subject_journal`
holds `decision`, `action`, `attempt` and `receipt` entries with the exact head,
generation, subject version and artifact; `(subject_id, idempotency_key)` is
unique, so `append_integration_subject_journal_on` returns the original entry
on replay. A trigger refuses UPDATE and DELETE. A CHECK requires a decision to
name its rule, primitive and facts digest, and an attempt or receipt its exact
head. Phase 2 may add receipts/attempts tables with `p2_parents`; until then
these entry kinds are the record.

**Closed outcomes plus a universal `unknown(reason)`.** `PRIMITIVE_OUTCOMES`
transcribes the §3.4 table (55 outcomes across 20 primitives). Every primitive
may also answer `unknown` with a reason (§3.1 principle 4, §5.4), so adapters
are total; the policy compiler should require every table to handle it.
`OUTCOME_DETAILS` names the parameters of `merged(head)`,
`conflict(member, files)`, `source_moved(member)`, `busy(holder)` and
`answered(choice)`. `PrimitivePorts.invoke` answers
`unknown(primitive_unavailable)` for an unbound primitive and
`unknown(contract_violation: …)` for an adapter that answers outside its
contract; other exceptions are the loop's to isolate.

**Shadow-safe primitives are declared.** `Primitive.mutates` is false only for
`integration_observe_subject`, `git_ancestry`, `record_decision` and `wait`:
the only primitives shadow mode may perform.

**The ports wrap existing commands.** `PHASE1_ADAPTED_COMMANDS` names the
command contract each phase-1 adapter calls as it is (§6 phase 1):
`integration_seal`, `integration_build_candidate`, `integration_ci_evidence`,
`integration_promote_main`, `integration_repair_start`/`_dispatch`,
`integration_eject`, `integration_cleanup`. No new command contract is
registered; phase 4 consolidates the surface.

**Natural keys.** `subject_key(kind, repository_id, *parts)` gives
`root_batch:<repo>:<request_id>`, `parent_episode:<repo>:<task>:<collection
generation>` and `source:<repo>:<task>:<review generation>`. Creation through
`ensure_integration_subject_on` is idempotent on `(project_id, kind,
subject_key)` and never overwrites an existing row. At most one `admitting`
root subject exists per `(project, repository)`
(`uq_integration_subjects_admitting_root`); how many sealed batches may coexist
stays policy. An unbound admitting root whose request is no longer outstanding
can never seal, so the seed for the replacing request closes it as `superseded`.

## Not in this task

Gates (`gates.py`, primitive 18) and writer leases (`writers.py`, primitives
11–13) are phase 2; `gate_id` and the writer columns carry their facts without
a foreign key so those owners can choose the backing rows. No subject is
created, visited or activated, and no existing table, command or doctor check
changes.
