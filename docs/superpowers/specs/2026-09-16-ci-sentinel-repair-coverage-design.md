# CI sentinel — a repair owns the failing tests it was filed for

Amends [`2026-09-05-ci-main-sentinel-design.md`](2026-09-05-ci-main-sentinel-design.md).
Task `nimble-nexus`, found while re-activating the sentinel (`stark-beacon`).

## Problem

`ci_baseline_status` recognised an existing repair only through its dedup key,
`ci-baseline:<signature>:<n>`, where the signature digests the sorted failing
pytest node ids. That fails twice:

1. **A repair filed by hand is invisible.** `steady-quest` (no dedup key,
   `IN_PROGRESS`, the same 120 failing tests) would have got a second
   `deep-high` repair, `ci-baseline:aa8f787cd293:1`, beside it. Nothing could
   attach a key to an existing task, so an operator could not hand the sentinel
   the repair already in flight.
2. **A partial fix looks like a new failure.** The signature changes whenever
   the failing set does: `bd172e13c` failed 120 tests (`aa8f787cd293`),
   `0bfda2594` failed 116 (`8ac673f8d816`). Every partial fix a repair lands
   while still in flight would earn a fresh "attempt 1" for what is left.

The original spec's rule — "a different failure gets its own task" — is right;
what was wrong is treating a subset of an in-flight repair's failure as a
different failure.

## Decision

**A repair owns the failing tests recorded on it.** "The same failure" is
decided by ownership, not by signature equality.

### The record

Task metadata `ci_baseline_repair` on the repair task:
`{ref, head_sha, signature, failing_tests, failing_checks, recorded_at}`.
It is written once, by a new command.

### `ci_repair_adopt` (write, `update` on `task`)

Arguments: `project_id`, `task_id`, and optionally `ref`, `head_sha`,
`failing_tests`, `failing_checks`.

- The task must be in the project and live (not `COMPLETED` / `FAILED` /
  `BLOCKED`), and its dedup key, if any, must be a `ci-baseline:` key.
- An unkeyed task is keyed `ci-baseline:<signature>:<n>` (`adopted`); a keyed
  one keeps its key (`recorded`); a keyed and recorded one is left alone
  (`unchanged`). The key is written before the record.
- With no failure named it reads `ref`'s CI now and adopts the whole failure —
  the operator path: `aq git ci-repair-adopt --project-id agent-queue --task-id
  steady-quest`. The sentinel passes the failure it just observed instead.

### Ownership in `ci_baseline_status`

For the current failure — its failing tests, or its failing checks when no test
ids could be read:

- A **recorded** live repair owns the tests it recorded, for the same `ref`.
  While logs are unreadable it owns the checks it recorded, so a `gh` hiccup does
  not file a duplicate. A repair recorded with no test ids owns every test its
  checks turn red.
- An **unrecorded** live repair (filed before this change) owns the failure only
  when its key's signature is exactly the failure's — the old rule.

Then:

- **Live repairs own every failing test** → reuse the oldest owner keyed on
  exactly this failure, else the oldest owner. A shrinking set keeps its repair.
- **Some tests are unowned** → a new repair for *those tests only*, keyed on
  their signature. Its description names the in-flight repairs that own the
  rest. A different failure still gets its own task, and no two fixers work the
  same test concurrently.
- **Prior attempts** are the spent repairs that owned *all* of the new repair's
  failure. So two fixers that each owned a set of tests and ended with some of
  them still red escalate that remainder to a human, even though each partial
  fix changed the signature. The new key's index is one past the highest index
  already used for its signature, so it can never collide with a spent key
  `ensure_task` would reuse.

`signature` in the result still digests the whole observed failure (for
attribution against a branch); `repair_signature`, `repair_tests`,
`repair_checks` and `in_flight` describe the repair decision, and the escalation
key uses `repair_signature`.

### The playbook

`keep-main-green` gains step 3 between `ensure_task` and the terminal: on
`created` **and** `reused`, `ci_repair_adopt` with `task_id: repair.task_id` and
`baseline.ref`, `baseline.head_sha`, `baseline.repair_tests`,
`baseline.repair_checks`. On a reused, recorded repair it is a no-op; on a
legacy unrecorded repair it backfills the record. Escalation becomes step 4.

## Alternatives rejected

- **One live repair per project, whatever its signature.** No record needed and
  no playbook change, but one stale repair would swallow every later red state,
  and a failure unrelated to the in-flight repair would wait for it to end.
- **A `dedup_key` field on `edit_task`.** Generic, but it lets any caller
  hijack another pipeline's key, makes the operator compute a signature by hand,
  and still gives the sentinel no way to see a shrinking set as the same repair.
- **Recording from `ci_baseline_status` itself.** It is contracted `read`, and
  a record written on the tick after filing misses a partial fix that lands
  inside that window.

## Rollout

The `ci_baseline_status` contract gained result fields and the playbook gained a
command, so the reviewed bundle `tests/fixtures/playbooks/v2/ci-main-sentinel/`
was rebuilt (`scripts/rebuild-reviewed-playbook-artifacts.py ci-main-sentinel`,
then `manifest.md` digests). An install re-imports and re-activates it:

    aq playbook v2-import --path reviewed-playbooks/ci-main-sentinel
    aq playbook activate --playbook-id ci-main-sentinel --artifact-sha256 <hash>

Before activating, adopt any repair already filed by hand.

## Tests

`tests/test_ci_baseline_status.py`: manual adoption then reuse, shrinking set
reuses, a new failure beside an in-flight repair gets a repair for just that
failure, spent owners of a shrunken failure escalate it, unreadable logs and
other refs, adoption refusals, and the pure `plan_repair` edges.
`tests/test_ci_main_sentinel.py`: the record step's bindings and transitions.
