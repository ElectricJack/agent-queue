# Quilt health watch review candidate

This is a disabled, offline candidate for project `quilt-trader`. It is not a
first-install default or an approved bundle. Nothing in this change imports,
installs, enables or activates it. The supervisor must review the exact source
and artifact before any later rollout.

## Baseline and changes

The supervisor supplied the installed bundle and historical draft scenarios to
task `prime-apex-38`. The installed source is authoritative, not the draft source
or the separately supplied project-playbooks copy. There was no shipped Quilt
source in this checkout. The installed artifact is version 2,
`sha256:68115d334888c6644b7799a93360a11918cd1bac4ab153b065ea995318eb77f7`.
Its immutable source, artifact, manifest and diagnostics are under
`tests/fixtures/quilt_health_watch/installed/`. The supplied draft check outputs
and dry-run goldens are under `drafts/`; `SHA256SUMS` pins their original bytes.
The historical goldens predate the installed worker-route migration and retain
their original artifact hash. Tests exclude only that identity when comparing
the execution trees; they do not rewrite the goldens.

Version 3 replaces two expanded eleven-clause schedule predicates with lexical
`filter` and `len`, and combines the quiet OK/KNOWN decision cases using the same
bounded collection primitives. The collection feature is pinned by the compiler.
The original execution authority is preserved (see the boundary below).
`enabled: false` is preserved. The market window remains 13:00–21:29 UTC,
including weekends and both Pacific offsets; this port deliberately preserves
the installed approximation. ALERT still wins over CLEARED, and both precede
OK/KNOWN, including summaries containing multiple verdict strings.

Typed `get_task` results retain the `read` outcome and raw
`completion.summary` path, with an empty-string fallback. `message_send` retains
`queued` and all refusal/failure edges. The three worker tasks, timeouts,
capability ceilings, exact check/commit instructions, subjects, recipients,
priorities and complete message bodies are unchanged. Catalogue receipts from
`aq playbook commands --name NAME --include-schema --json` for all three commands
are recorded in `command-catalogue.json`; offline compilation also validates
against this checkout's actual contracts and registered event schemas.

## Deliberately retained boundaries

`required_capabilities` is deliberately deferred. Declaring the three commands
creates `trusted_execution` metadata and switches effects to a PLAYBOOK principal
whose artifact policy grants no harness tools. The retained child tasks require
Bash/Read/Grep/Glob: the real delegation check rejects that policy. A regression
test reproduces this refusal. Leaving the declaration absent preserves the
installed authority path; it does not add grants. This follows the supervisor's
instruction to defer the declaration if it changes execution. Enabling run-bound
authority requires a separate reviewed delegation design before rollout.

The external Quilt worker still runs `scripts/health_watch.py` and files its
offered error signatures. That script's code, runtime settings, repository pin
and local-state schema were not supplied. Its workspace, service endpoints,
watchdog permission, credentials and check/commit protocol cannot be inferred
from recorded JSON. A daemon script runner would execute in a different context;
it is not an equivalent replacement for this task boundary.

The existing script owns log offsets, signature deduplication and hourly aggregate
alert/reminder/clear decisions. Adding AQ notification state would create a second
state owner, and the summary alone does not provide complete condition coverage
or pre-scan revisions for safe clears. State-notify also does not expose the
existing alert/recovery priority fields. The explicit `message_send` steps are
therefore retained. No state schema version or script manifest is needed.

Doctor's task observations do not probe Quilt coordinator, worker or deployment
health. This policy has no daemon-wide task scans, so doctor snapshots/paging,
`foreach` and absence-based clears would not replace an existing operation.
The script's at-most-five signature loop remains in its existing transaction
boundary. No runtime mechanism or other playbook is changed.

## Rebuild and evidence

From the repository root:

```bash
python tests/fixtures/quilt_health_watch/candidate/quilt-health-watch/build.py
python tests/fixtures/quilt_health_watch/candidate/quilt-health-watch/build.py --check
aq test tests/test_quilt_health_watch.py
```

The local builder uses `PlaybookSource`, `propose` and `validate_definition`
against the normal command/event/profile registries. It writes only this candidate
directory and preserves the recording timestamp on rebuild. Its input is the
reviewable `semantic-body.json`; it never invokes the daemon or imports a bundle.
This optional candidate is not registered in the shipped reviewed-bundle rebuild
inventory. The only compiler diagnostics are three informational reminders that
the child route's capability ceiling is checked at run time.

The focused tests drive the real engine with simulated external worker completion
and typed read previews. Real command invocation fails the test. Coverage includes:

* all ten supplied dry-run goldens without changes;
* fifty new event, summary, task-failure and read-failure scenarios, comparing
  old/new trees and exact queued message inputs;
* every minute of a winter weekend and summer weekday for both complementary
  timer rules;
* byte-identical baseline evidence, clean compilation and all unchanged steps;
* the harness-tool delegation refusal introduced by command capability metadata.

Recorded focused result: **65 passed**. The tests used the disposable PostgreSQL
test server on port 5534 via `POSTGRES_TEST_DSN`; the operator database was not
used. The offline `build.py --check`, changed-file Ruff and `git diff --check`
also passed.

Related area checks passed (**197 tests**):

```bash
aq test tests/test_playbook_collections.py tests/test_playbook_v2_compiler.py \
  tests/test_list_playbooks_v2.py tests/test_selection_catalogue.py \
  tests/test_agent_task_executor.py -q
```

These are deterministic graph equivalence checks, not a live Quilt health check.
They do not run the watchdog, contact trading services, file tasks or send alerts.
Live registry and execution readiness must be checked by the operator at rollout.
