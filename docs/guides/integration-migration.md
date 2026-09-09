# Switching integration modes

Moving a project between AQ's delivery modes — into development mode, back out
of it, or on to one of the optional strict modes — without losing history or
stranding work in flight.

Read [the integration concept page](../concepts/integration.md) for what each
mode actually does. This page is only about the transition.

## The modes, and how you reach each one

| Target mode | Command |
|---|---|
| `development` | `aq integration develop <project> --validation … --reason …` |
| `disabled` | `aq integration enable <project> --mode disabled --expected-generation N --reason …` |
| `observe` | `aq integration enable <project> --mode observe --expected-generation N --reason …` |
| `hierarchy` | `aq integration enable <project> --mode hierarchy --expected-generation N --reason …` |
| `train` | `aq integration enable <project> --mode train --expected-generation N --reason …` |

`aq integration enable` deliberately does **not** accept `development`:
development mode carries a validation policy, so it is configured rather than
switched. `aq integration develop` is the only way in, and it works from any
current mode.

Both commands require local operator authority — a worker session's token is
refused ([`src/commands/integration_commands.py`](../../src/commands/integration_commands.py)).

## Generation: the compare-and-swap token

Every project carries `hierarchical_integration_generation`, reported by
`aq integration status <project>`. Any mode change quotes the generation it
believes it is changing from:

```bash
aq integration status demo
```

```text
{"effective_mode": "development", "desired_mode": "development", "generation": 14, …}
```

```bash
aq integration enable demo --mode observe --expected-generation 14 --reason 'Staging a strict rollout'
```

If the project changed underneath you, the command answers
`{"outcome": "stale", "generation": <current>}` and writes nothing. Re-read the
status and decide again — never retry with a guessed number. The same guard
applies to `aq project set … integration-repository-id`, which requires
`--expected-integration-generation`.

Each successful change increments the generation and appends a row to
`integration_rollout_transitions`, so the history of who changed the mode and
why is durable.

## Into development mode

```bash
aq integration develop demo \
  --validation focused \
  --command '/path/to/venv/bin/python -m pytest -q' \
  --reason 'Batched development delivery'
```

In one transaction
([`DevelopmentIntegration.configure`](../../src/integration/development.py)):

1. The publication repository is resolved. With exactly one registered
   repository it is used; with none, one is created from the project's
   `repo_url`; with several, the command refuses until you designate one.
2. The project's effective **and** desired mode become `development`, any drain
   flag is cleared, the generation is bumped and the policy is stored.
3. A legacy-suppression row is written turning off the older automation for
   this project: the `pr-merge-sweep` playbook, legacy final-review routing and
   legacy gate creation.
4. A `configuration` row goes into the delivery journal recording the operator,
   the reason and the exact policy.

Nothing about the strict modes' tables is deleted. Existing batches, candidate
revisions, repair operations, CI evidence and attestation publications remain
as audit history.

### The switch is blocked

```text
blocked: legacy default-branch mutation must be reconciled first
```

`configure` refuses while the old machinery still has an unfinished write to
the default branch: a promotion intent for that branch in a state other than
`committed`, `conflict` or `superseded`, or a branch-owner row for it that is
not `released`. Switching publishers underneath an in-flight write is how a
push gets lost, so it is refused rather than forced.

To clear it:

```bash
aq integration status demo
```

and then resolve the named work with the strict-mode controls —
`aq integration resume <operation>` to let it finish,
`aq integration abort <operation> --reason …` for one that cannot, and
`aq integration retry-cleanup <batch>` for cleanup that stalled. When the
project reports no active work, run `aq integration develop` again.

## Out of development mode

```bash
aq integration enable demo --mode disabled --expected-generation 15 --reason 'Pausing managed delivery'
```

What happens:

* The mode changes and the generation is bumped. Sweeps stop: the development
  tick only selects `ACTIVE` projects whose mode is `development`.
* The delivery journal is **retained**. It is history, not runtime state.
* Candidate refs under `refs/heads/aq/development/…` are retained too. They are
  ordinary refs on your remote; delete them yourself if you want them gone.
* Repair tasks that were already filed stay on the queue as ordinary tasks.
  Close or cancel them if they are no longer wanted.

Switching to `disabled` while managed work is still active does not stop it
mid-flight: the project is put into **draining** (`{"outcome": "draining"}`),
keeping its effective mode while the desired mode becomes `disabled`, and the
drain reconciler finishes what is in flight before the change completes.

## Into `hierarchy` or `train`

These are optional compatibility modes, off by default, and they are gated on a
functional preflight rather than on your certainty:

```bash
aq integration enable demo --mode hierarchy --expected-generation 15 --reason 'Adopting parent-owned delivery'
```

```text
{"outcome": "blocked", "blockers": [{"code": "…", "detail": "…"}], "blocker_digest": "sha256:…"}
```

A blocked result lists exactly what is missing — repository configuration,
dependencies the runtime path needs, playbook routes, and historical state the
new mode cannot interpret
([`preflight.py`](../../src/integration/preflight.py),
[`controls.py`](../../src/integration/controls.py)). Fix the named blocker and
try again.

For **historical** blockers only — the ones that describe state left by an
older configuration rather than something broken now — an operator may waive
exactly what was reported:

```bash
aq integration waive-history demo --blocker-digest sha256:… --reason 'Reviewed; pre-rollout PRs are closed'
aq integration enable demo --mode hierarchy --expected-generation 15 --waiver-id <id> --reason '…'
```

The waiver is bound to the exact blocker digest and is single-use: a waiver
whose digest no longer matches, or that has already been consumed, is refused.
There is no general override.

`train` additionally takes `--interval-seconds` for its sweep cadence, and only
`train` accepts it — passing it with another mode is an error, and it cannot be
changed while the project is draining.

Going from `hierarchy`/`train` to `observe` requires that managed work has
drained first; the command answers `blocked` with `active_integration_work`
until it has.

### Tasks that pre-date the rollout

Tasks created before hierarchy mode was enabled have no reserved branch origin,
and claiming in hierarchy mode is gated on one:

```bash
aq integration reconcile-unmaterialized demo --expected-generation 16 --reason 'Bind pre-rollout tasks'
```

That binds the safe pre-rollout tasks and reserves their origins;
[`BranchMaterializationService`](../../src/integration/branch_materialization.py)
then cuts the branches from the integration loop.

## What a mode change never does

* It never rewrites or deletes worker history. Task branches, candidate refs
  and per-parent aggregates are left alone.
* It never discards the delivery journal or the strict modes' audit tables.
* It never fabricates evidence. Work that reached `main` outside AQ is recorded
  with `aq integration adopt`, which labels it operator acceptance, not CI
  attestation.

## Schema and downgrades

Development mode's journal arrived with Alembic revision
[`a0000000000c_development_integration`](../../migrations/versions/a0000000000c_development_integration.py).
Its **downgrade refuses** to run while any `development_deliveries` row exists,
or while any project is in (or wants) `development` mode:

```text
RuntimeError: development delivery history exists; retain the journal
```

That is the guard working: a downgrade would drop the audit trail of what
reached your default branch. Take the projects out of development mode and
decide explicitly what to do with the journal first.

> **Warning.** Migrations are daemon-only. Never run `alembic upgrade`,
> `alembic stamp` or `aq start` from a worktree slot — see
> [migrations](migrations.md). `aq db current` is the safe read-only answer to
> "am I behind?".

## Related pages

* [Integration](../concepts/integration.md) — what each mode runs.
* [Development integration](development-integration.md) — operating the mode
  you most likely want.
* [Integration troubleshooting](integration-troubleshooting.md) — when a
  transition or a delivery is stuck.
* [Migrations](migrations.md) — who may run Alembic against which database.

## Source and tests

[`src/integration/controls.py`](../../src/integration/controls.py),
[`src/integration/development.py`](../../src/integration/development.py),
[`src/integration/preflight.py`](../../src/integration/preflight.py),
[`src/cli/integration.py`](../../src/cli/integration.py).

```bash
aq test tests/test_integration_operational_controls.py tests/test_integration_controls.py tests/test_development_integration.py
```
