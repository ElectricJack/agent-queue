# Guarded manual integration

Authority: approved `rev-fair-lantern`, revision 2, §11.2 (canonical SHA256
`4518d9296649ddc2f0192ff73dd682864fb938e3d9687069282b4f6b0a86453f`), and the
supervisor's bounded implementation direction on `vivid-lantern-50`.

`aq integration off PROJECT` previews an operator transition to the existing
`disabled` effective/desired modes. `--apply --expected-plan-digest DIGEST`
applies only that exact preview. Manual/off is a surface name, not a new
persisted mode. The command is local-operator-only, increments the policy
generation, disables automatic scheduling and records its plan and principal.
It preserves tasks, claims, human holds, frozen batches, candidates, operations,
sources and attempts. It does not cancel running CI or writers. Publication
already inside its bounded critical section finishes before off commits;
subsequent train publication and admission are refused. Ordinary worker source
publication and task execution remain available. Existing generation-fenced
project policy configuration owns explicit re-enabling.

The qualified helpers are `ManualIntegration(db, clock=...)` and
`run(IntegrationOffArgs, operator_id=...)`, `enabled_train_write(db, repository_id)`,
`train_write`, and `fence_disable`, in `src/integration/manual_control.py`.
The off plan binds the repository URL, default target, policy generation,
effective/desired modes, draining state and operator reason. Configuration drift
refuses apply. The generic CommandHandler/execute API and existing integration
status expose the same persisted disabled representation. This adds no schema.

The supervisor's scope corrections assign approved revision 2 §11.1/11.2 to
`stark-willow-95.2`. Its immutable one-time disposition must use existing
`system_config` storage, preserve original source identity, require a precise
preview digest and local permission, and never answer contained or manufacture
PASS. Equivalent-only legacy generations may be lifecycle-retired under the
approved `delivered_by_other_means` contract; later equivalents remain pending,
and unknown originals remain blockers. The owner must consolidate consumers,
audit sinks and acceptance. No second ledger or migration belongs in this leaf.

The provisional legacy implementation drafted under the earlier direction is
preserved in published commit `3c92251c63adc764099b6ac3683c9f84013c851c`.
Its retirement method, consumer changes, schema/migration and tests are source
handoff only, superseded on this branch by the qualified off/manual scope.
They are not approval to apply a live cutover or claims of the owner's acceptance.
Original Matter legacy records, live equivalence acceptance, policy/ruleset
changes and train re-enabling remain unfulfilled and outside this leaf's authority.
