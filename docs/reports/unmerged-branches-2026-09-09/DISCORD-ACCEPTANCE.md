# Discord simplification acceptance

Implementation task: `noble-ridge.12`; epic: `noble-ridge`.

The consolidated source includes the exact tips of children `.1`–`.11`. The old retained repair `de34e965` had a tree identical to `.11`; its missing ancestry was recorded without changing that tree, then the latest `.10` lifecycle fixes were merged. No child implementation was discarded.

## Product contract and evidence

| Requirement | Evidence exercised locally |
|---|---|
| One configured channel, escalation threads | Escalation delivery and lifecycle tests cover root/thread creation, partial creation reconciliation, reply routing and permission failures. |
| Hourly brief updates only when work is happening | Digest tests cover one hourly message, silent idle windows, ongoing/late work, generation changes, bounded catch-up and no repeated facts. |
| Blocked task → supervisor → Discord reply → supervisor recovery | Lifecycle tests cover blocked-attempt recovery across supervisor/daemon restart; failed recovery remains open in the same thread. |
| No direct Discord task/gate/worker mutation | Retired-input lifecycle regression and cutover tests confirm those inputs cannot mutate work; late authorized replies cannot reopen archived tasks. |
| Discord outages do not stop scheduling | Outage, rate-guard and disabled-Discord lifecycle tests preserve the scheduler, event bus and dashboard escalation commands. |
| Migrate pending conversations and remove obsolete surfaces | Cutover tests preserve old evidence, reconcile pending conversations once, validate destination choice and clear legacy interactive views. Documentation scan checks retired surfaces. |
| Dashboard replacements | Messaging and escalation inbox component tests: 9 passed. |
| Schema and typed clients | New Discord revisions follow operator recovery revision8 as revisions9/a/b. Escalation action migration upgrade/downgrade passed on an isolated PostgreSQL database; offline Python and TypeScript clients regenerated, API-client contract and documentation checks passed. |

## Validation record

- Lifecycle/digest/delivery/cutover/documentation subset: 77 passed, one historical-audit wording failure corrected.
- Corrected documentation plus generated API client contracts: 18 passed.
- Dashboard messaging and escalation inbox: 9 passed.
- Migration checks: two candidate/conflict schema checks passed; corrected escalation-actions PostgreSQL upgrade/downgrade check passed separately.
- The isolated swarm initially caught obsolete `--no-auto-create-channels` in the fixture project bootstrap. That flag was removed; final swarm result and main delivery SHA are recorded in `EXECUTION.md`.

## Rollout

Use the migration runbook in `docs/guides/discord-migration.md`, configure the single shared channel, and inspect cutover status before enabling external delivery. The daemon refuses to invent a destination when legacy channel choices conflict. Production migration/restart and AQ record reconciliation are recorded in `EXECUTION.md`.

This is automated acceptance using simulated transport and real isolated PostgreSQL/daemon execution. No test messages were posted to a live Discord channel and no live human reply round trip is claimed.
