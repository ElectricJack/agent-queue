# Provider failover: end-to-end acceptance run (2026-09-21)

<!-- aq:historical -->
> **Evidence, not guidance.** This bundle records what one acceptance run of
> provider failover observed on 2026-09-21, for task `bold-rapids.5`. For what
> to do during an outage, read [the runbook](../../guides/provider-outage.md);
> for how failover schedules work, read
> [scheduling](../../concepts/scheduling.md#provider-availability-and-failover).

## What was run

The Tier 1 end-to-end kit ([e2e swarm](../../guides/e2e-swarm.md)) ran
against a real daemon on real PostgreSQL, through the real `aq` CLI, with no
LLM. It was a fresh `scripts/e2e-env.sh --reset`, then all sixteen scenarios.
Scenario **S16 — provider failover** is the check the design asks for
([provider failover](../../specs/provider-failover.md) D23, "End to end"). It
uses the fake provider kit in `tests/fixtures/provider_failover/`: two fake
harnesses, `prova` and `provb`. A class, `std-high`, runs on both; a
`solo-high` class runs on `prova` only, as the `astra-high` analogue. Each
has a `max_active: 1` pool profile. S16 logs the fakes out and restores
them by rewriting `sessions.fake_script_file`.

* Base commit `cf7c10942` plus the uncommitted `bold-rapids.5` working tree —
  the change set this bundle ships with.
* Isolated world: its own home, API port `8117`, database `aq_e2e_br5` and
  tmux socket. The transcript prints the home as `$AQ_E2E_HOME` (the only
  edit to it).
* `playbooks.enabled: false`, as in all of Tier 1. The re-route sweep is
  therefore driven by `aq provider reroute`, the operator form of the command
  the `provider-failover` playbook calls. Holds on tasks the sweep would move
  read `failover_inactive` until it runs.

Reproduce:

```bash
scripts/e2e-env.sh --reset
scripts/e2e-daemon.sh start
scripts/e2e-smoke.sh            # or: scripts/e2e-smoke.sh S16
```

## Result

**16/16 scenarios passed**; S16 took 206 s. The full output, plus the provider
status, transition history, escalations and notices read back afterwards, is
in [`transcript.txt`](transcript.txt).

| D23 end-to-end requirement | Observed in S16 |
|---|---|
| Queue three `preferred`, one `pinned`, one `solo-high`, two `class_only` tasks | Created with the intents asserted from `aq task show` (`--pin` → `pinned`, an explicit profile → `preferred`, `--provider-intent class_only` edit). |
| Set `prova` to `login_required`; state within two launches | `unauthenticated` (`login_required`) after 2 launches, read from the provider's `startup_dialog` evidence. |
| No further launches | None in the next 12 s, a little over two cascades. |
| Moves inside `provb`'s `max_active: 1` | The dry run planned one move, and the live sweep moved exactly that task. The top-up sweep moved the next one into the same batch, `prb-prova-2`. `provb` never ran two sessions. |
| Holds with their kinds | `provider_pinned` (the pin), `no_equivalent_rung` (the solo-high task), `awaiting_failover_capacity` with `ahead` in the sweep, and `failover_inactive` in `aq provider held-tasks` before it ran. `aq task explain` reports `provider_hold`. |
| One supervisor message | One outage notice to `user:dashboard` / `session:supervisor-global`, and one batch notice to `supervisor-e2e`, which a later top-up did not repeat. |
| One escalation | `escalation-provider-prova-2`, `high`, `needs_human`. It was resolved automatically when `prova` recovered. |
| `aq provider status` | State, reason, since, `held 5`, `rerouted 2`, `batch prb-prova-2`. |
| Restore `prova`, `aq provider recheck` ⇒ probation ⇒ `available` after one launch | The probe answered `authenticated`, putting `prova` in `degraded` (`recovering`) on probation. The single canary launch claimed a held task, and `prova` went `available`. |
| Held tasks run on `prova`; moved-and-queued stay on `provb`; `reroute-undo` returns one | The pinned and solo-high tasks were claimed by `prova` sessions. The queued moved task stayed on `std-high-provb` until `aq provider reroute-undo --task-id` returned it. |
| Both providers down ⇒ holds, no moves, critical escalation | `prova` was logged out and `claude`, `codex` and `provb` were disabled. The sweep moved nothing and held 5 as `all_providers_unavailable`. The escalation went `critical`, and doctor `providers.availability` reported `error`. |
| … `not_admissible` | A live claude session's claim answered **`drain_requested`**, not `not_admissible` — see finding 5. |

## Dashboard, against the same daemon

These screenshots were taken from the development dashboard
(`scripts/e2e-dashboard.sh`) pointed at the e2e daemon during a staged `prova`
outage:

| File | What it shows |
|---|---|
| [`dashboard-metrics.png`](dashboard-metrics.png) | The outage banner on every page ("Prova unavailable — logged out since 02:00 · 1 task moved · 6 held"). The provider cards show a state pill, reason, since, held and re-routed counts, remediation, and the *Disable for…* / *Recheck* actions. |
| [`dashboard-task-rerouted.png`](dashboard-task-rerouted.png) | A moved task: its *Preferred* intent chip, "Re-routed from `std-high-prova` to `std-high-provb` · undo", and the system comment. |
| [`dashboard-undo-refused.png`](dashboard-undo-refused.png) | Undo refused while `prova` is still unavailable, with *Undo anyway*. Pressing it restored the route (`operator_undo` recorded). |
| [`dashboard-task-pinned-hold.png`](dashboard-task-pinned-hold.png) | A pinned task's *Held by provider* block: the kind in words, the provider state, since, and remediation. |
| [`dashboard-tasks-held.png`](dashboard-tasks-held.png) | The Tasks tab's *Held by provider* filter, with a per-kind summary and each row's reason. |
| [`dashboard-disable-override.png`](dashboard-disable-override.png) | *Disable for… 30 minutes* applied to `provb` from the card. It shows the override badge with expiry and author, *Clear override*, and a second banner line. |

## Findings

Fixed in `bold-rapids.5`, because the run could not pass without them or an
operator would hit them in the first outage:

1. **The router could not place a vendorless harness.** `_class_model`
   (`src/agents/routing.py`) lacked the id-keyed slice fallback that
   `SessionSpecBuilder` launches with. Any operator-authored harness with no
   vendor was refused ("has no model for harness").
2. **Every outage notice, escalation and doctor detail suggested a command the
   CLI rejects.** It printed `aq provider recheck codex` where the CLI wants
   `--provider codex`. The text now uses the real flags.
3. **A fresh daemon refused `aq provider set-state claude …` for a minute**
   ("unknown provider; known providers: none"). The tracked-provider set was
   read before the profiles synced and then cached empty. An empty read is no
   longer cached, and an unknown name re-reads once before refusing.

Not fixed here (none Critical or High; recorded for triage):

4. A probation canary that ends without success or failure evidence (it is
   killed, or it dies after startup for an unrelated reason) is not released.
   The provider then admits no launch for `CANARY_TIMEOUT_SECONDS` (10 min).
   Filed as `grand-current`.
5. D15 says a pool claim during an all-providers outage answers
   `not_admissible` / `provider_unavailable` with a wait hint. The code answers
   `drain_requested` for any session whose own provider is suppressed
   (`src/commands/claim_commands.py`). The outcome is safe, but it is not the
   documented one. Filed as `solid-pinnacle`.
6. `aq provider status`'s held count (`affected()`) counts only `READY` tasks,
   against the raw project default. `aq provider held-tasks` and the per-task
   hold also cover `DEFINED`, `BLOCKED` and `PAUSED`, so the two can disagree.
   Filed as `bright-delta`.
7. The task-attachments API accepts images only, and refuses a pool session's
   token for the task that session holds. So this transcript is committed here
   rather than attached to the task. Filed as `grand-harbor`.
