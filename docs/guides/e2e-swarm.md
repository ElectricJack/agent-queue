---
tags: [guide, testing, swarm, e2e]
---

# End-to-end testing the swarm work model

The unit suite proves each piece of the swarm work model in isolation. This
kit proves they compose: a **real daemon**, a **real PostgreSQL database**,
the **real `aq` CLI**, and the real claim protocol — start to finish, with
nothing mocked but the thing that spawns processes.

It comes in two tiers.

| | Tier 1 | Tier 2 |
|---|---|---|
| `sessions.provider` | `fake` | `tmux` |
| Who is the worker | `scripts/e2e-smoke.sh` | a real `claude` process |
| Command-only playbooks / supervisor | on / off | on / on (a live agent needs the supervisor) |
| Durable messages | local sink only | on |
| Runtime | ~4 minutes | as long as the model takes |
| Deterministic | yes | no |
| Costs tokens | no | yes |
| Use it for | every change to claims / pools / formulas / hierarchy | before shipping a change to the bootstrap prompt or the harness |

Tier 1 is the one you run. Tier 2 is the one you run when you have changed
something an LLM has to read.

Everything lives under `$AQ_E2E_HOME` (default `~/.agent-queue-e2e`) and in
the `agent_queue_e2e` database. **Your real `~/.agent-queue` and the
`agent_queue` database are never touched** — different data dir, different
vault, different database, different API port (8099), different tmux socket
(`aq-e2e`).

## Prerequisites

```bash
docker compose up -d postgres          # the dev PostgreSQL on :5533
docker compose up -d postgres-test     # managed validation's disposable test server on :5534
pip install -e ".[dev,cli]"            # and packages/aq-client
git --version                          # any recent git
```

Tier 2 additionally needs `tmux` and a `claude` binary on `PATH`.

The generated config enables managed jobs for development validation. Its test
maintenance DSN uses `POSTGRES_TEST_DSN` when supplied, otherwise the compose
`postgres-test` server on `:5534`; it is separate from the e2e daemon database.

## Tier 1 — the scripted run

```bash
scripts/e2e-env.sh --reset
scripts/e2e-smoke.sh
```

`e2e-smoke.sh` starts the daemon if one is not already up and stops whatever
it started — including on Ctrl-C and on a failing scenario. To keep a daemon
across several runs (much faster while iterating), start it yourself:

```bash
scripts/e2e-daemon.sh start
scripts/e2e-smoke.sh S2 S4          # just these two
scripts/e2e-daemon.sh logs 200
scripts/e2e-daemon.sh stop
```

The default run executes all 20 scenarios. `S1`–`S8` cover swarm composition;
`S9`–`S14` cover the wider stateful CLI surface; `S16` runs a whole provider
outage against two fake providers; `S17` cooks and works a phased graph; and
`S18` proves the reviewed supervisor failure-triage playbook against a real
terminal worker close. `S19` uses a planner's authenticated session token to
file a graph, prove root/cross-project/foreign-parent denials, race two graph
batches at quota, and work a graph-seeded checklist through prime and close.
Every CLI subprocess is
forced back to this disposable data directory and database even when the
caller is a worker carrying production-refusal sentinels.

S19 pauses scheduling in its disposable project before filing the checklist
graph, writes the operator override to the fixture pool, and releases that
pause in `finally` before claiming the child. Graph filing can dispatch the
router immediately; its routing gate alone cannot hold the child until the
override. Production still refuses overrides of claimed or running tasks.

S2, S3, S5, S18, S20, both S19 fixture claims and S16's three recovery claims
(the `provb` worker, the probation canary and the pinned task) retry
`no_ready_work` within the convergence deadline: PostgreSQL `SKIP LOCKED` can
skip a READY fixture while another transaction holds its row. Other claim outcomes and claims of an unexpected
task fail immediately. S2 and S3 accept only the remaining S1 fixtures, including
when separate pytest items run them in different subprocesses. A timeout reports
the last claim and the fixture state.

CI covers all 20 scenarios as **21 individually reported pytest items** in four
parallel `e2e-cli` shards: `claims` (S1–S3, S7, S19), `cli` (S5, S8–S9, S12,
S17, S20), `graphs` (S10, S16b, S18, S6), and `failover` (S4, S11, S13–S14, S16a).
S16a covers outage detection/rerouting; S16b prepares a separate outage through
public commands and covers recovery/undo/all-down. Select `S16` in the shell
runner to run the original full serial transcript.

Each shard uses one module-scoped disposable world. S1–S3 explicitly share state;
selecting S2 or S3 alone prepares its missing predecessors. Other item boundaries
remove tasks (including terminal rows) and sessions and restore swarm/provider/
playbook switches. Failed cleanup prevents further use of that world, and fixture
teardown always destroys its owned resources. No daemon is shared across workers.

Each item has its own 540-second pytest limit. The scenario step has an
eleven-minute deadline and a cache-miss dependency install has a separate
twelve-minute deadline; the 26-minute job budget is their sum plus setup and
post-job cleanup. The original hosted graph scenarios consumed 261 seconds; a
five-minute total cancelled that passing run during cleanup. Run 36830122857
spent 439 seconds installing from a slow PyPI and a shared ten-minute total then
cancelled the passing `cli` group, so neither phase can spend the other's time.
To reproduce a shard or selected scenario:


```bash
# One shard, matching CI:
aq test tests/test_e2e_cli_stateful.py -k 'claims-' -m integration -s
# One scenario, with independent setup and teardown:
aq test 'tests/test_e2e_cli_stateful.py::test_disposable_daemon_scenario[graphs-S16b]' -m integration -s
# Four parallel shards; loadgroup preserves fixture reuse:
aq test tests/test_e2e_cli_stateful.py -m integration -n auto --dist loadgroup -s
```

The shard selector includes the trailing hyphen from the scenario IDs (for
example, `cli-S5`). Using `-k cli` also matches the module's name and selects
every scenario instead of just the CLI shard.

Pytest uses a one-second periodic scheduler backstop, a half-second config
watcher, one-second graph sweeps and quarter-second condition polls.
`AQ_E2E_CYCLE_SECONDS` and `AQ_E2E_CONFIG_POLL_SECONDS` override the test launcher;
operator and tmux cadence use the production scheduling configuration defaults. Negative provider assertions
observe two **completed** scheduler cycles rather than assume elapsed time implies
scheduling. `src.main` daemons started manually retain the original 12-second
observation window.

Pytest preloads the full CLI once in a private Unix-socket launcher and forks a
separate CLI process for each ordinary command. Click parsing, command routing,
REST, per-invocation environment, output formatting and process exit codes all run.
Claim/graph races, waits, plugin startup probes, help and version use fresh
interpreters. This optimization is POSIX-only and opt-in through
`AQ_E2E_CLI_SOCKET`; the standalone shell runner continues to launch fresh
interpreters. Fixture registration, cleanup and polling use the public command API.

`TIMING` JSON rows report lifecycle, CLI call counts/durations, condition waits and
provider recovery. Set `AQ_E2E_TIMINGS_DIR` to keep per-shard JSONL files outside
the disposable home; CI uploads these with JUnit results. Condition durations
include their predicate cost and may overlap CLI time. The
[measurement and lifecycle audit](../reports/2026-09-30-e2e-shared-fixtures.md)
records comparable before/after results and the remaining startup coverage.

Scenarios S5 and S18 wait for their exact fixture to be claimed, rather than
assuming a `READY` task is available to one claim attempt. PostgreSQL's
`SKIP LOCKED` can temporarily return `no_ready_work` while another transaction
holds the task row. These scenarios retry that result within the convergence
budget; other claim outcomes and a claim of a different task fail immediately.
Timeouts report the last claim response and the fixture's current route and
status. S18 still requires the terminal failure, completed triage playbook and
durable supervisor notice after claiming its fixture.

A clean run:

```
swarm e2e (Tier 1) — daemon at http://127.0.0.1:8099

PASS S1 pool sizing (8.7s)
     2 sessions for 3 ready tasks; pool.scaled = 'start 2 worker'
PASS S2 claim loop as a worker (20.1s)
     claimed 1/1 then drain_requested; p-worker--e2e--cc2b0e3f retired, replaced by p-worker--e2e--ec70910c
PASS S3 worker-filed work (13.4s)
     stark-summit DEFINED + discovered-from calm-journey + routing gate gate-028dab6bd36e resolved
PASS S4 formulas (26.9s)
     cooked brisk-willow (brisk-willow.1, brisk-willow.2); as-cooked matches; settled as COMPLETED
PASS S5 fence + scope (30.3s)
     cross-session heartbeat and cross-project prime both refused (grand-forge)
PASS S6 doctor (12.2s)
     11 swarm checks clean; hot-reload flip warned (...) and restored (...)
PASS S7 PostgreSQL claim race (26.4s)
     outcomes ['no_ready_work', 'claimed'] — one winner, loser said no_ready_work
PASS S8 project onboarding (1.2s)
     real CLI linked an unchanged repository and initialized main + README; each project has one enabled project-repo workspace and standard vault storage
PASS S9 task lifecycle (...)
     partial flags exited 2 without persistence; full JSON receipt yielded [...]; update persisted; priority failure rolled back; task deleted
PASS S10 workspace + file/git/note writes (...)
     workspace/file/git/note CRUD persisted across processes; refusals rolled back
PASS S11 messages (...)
     queued/injected/replied through database-only sink; bad target refused
PASS S12 MCP registry (...)
     registry create/read/delete passed; loopback:1 probe = dependency-unavailable
PASS S13 plugin extensions (...)
     disposable entry point loaded when present and was an exit-2 unknown command when absent
PASS S14 graph + vault (...)
     layout rebuild/tidy persisted through daemon; isolated vault migration preview made no writes
     local validation and exact Git publication through real AQ CLI; operator adoption recorded without CI fabrication
PASS S16 provider failover (206.1s)
     prova tripped in 2 launch(es); moved …,… to provb within max_active=1 (batch prb-prova-2); pin/solo held; recheck→probation→available; undo returned …; all-down held everything, claim=drain_requested, critical escalation
PASS S17 phased graph (...)
     dry-run/cook phase graph; phase 2 withheld then released after phase 1 COMPLETED; prime rendered and close skipped 3 checklist rows

PASS S18 supervisor failure triage (...)
     task.failed completed the reviewed playbook run and queued one durable supervisor notice

18/18 scenarios passed
```

The runner exits non-zero if any scenario fails. It then prints a capability
report whose statuses are deliberately finite: `passed`, `broken`,
`unsupported`, `dependency-unavailable`, and `explicitly-untested`. This keeps
an absent optional service or retired surface from being reported as either a
false pass or a product regression.

Tier 1 keeps assignment deterministic without an LLM: its generated execution
profiles carry fixed `default_class` values, and the runner files each task
with hints only, then writes the route the project's router would write — a
`router` route whose candidates are every enabled profile of the class —
straight onto the row (`route_task` in `scripts/e2e/smoke.py`). S3, S16 and S19
use the operator's audited `aq task route-override` instead. This mirrors a
completed routing decision and makes pool claims exercise the live session's
class and model constraints.

### What the pieces are

| File | What it does |
|---|---|
| `scripts/e2e-common.sh` | shared paths, ports and DSNs; every value overridable from the environment |
| `scripts/e2e-env.sh` | builds the world: dirs, bare git repos + workspace clones, an onboarding root, an opt-in plugin entry point, vault fixtures, `bin/aq`, config, and database |
| `scripts/e2e-daemon.sh` | `start` / `stop` / `status` / `logs` for the isolated daemon |
| `scripts/e2e-clean.sh` | validates path ownership and the isolated tmux socket before any side effect, then stops the disposable daemon, drops only its database, and removes only its data directory |
| `scripts/e2e-smoke.sh` | the Tier 1 runner (thin wrapper) |
| `scripts/e2e/smoke.py` | the 18 scenarios and capability report |
| `scripts/e2e/aq.py` | runs *this worktree's* `aq` — see below |
| `scripts/e2e/register.py` | creates the `e2e` / `other` projects + their workspaces (needs the daemon) |
| `scripts/e2e/dbsetup.py` | creates/drops `agent_queue_e2e` via asyncpg (no `psql` needed) |
| `scripts/e2e/probe.py` | asks `/ready` whether the daemon can actually use its schema — the readiness gate `e2e-daemon.sh` and `e2e-smoke.sh` refuse on |
| `scripts/e2e-dashboard.sh` | the React dashboard pointed at the e2e daemon, for watching a run |

### Watching a run in the dashboard

```bash
scripts/e2e-daemon.sh start
scripts/e2e-dashboard.sh            # http://127.0.0.1:5173, proxied at :8099
scripts/e2e-smoke.sh
```

It needs the repo's `node_modules` and a generated TS client
(`npm install`, `./scripts/regenerate-ts-client.sh --from-file`) — in a
fresh worktree those are absent, and the quickest workaround is to run
Vite from a checkout that already has them:

```bash
cd /path/to/main/checkout/dashboard
AQ_API_TARGET=http://127.0.0.1:8099 npx vite --port 5174
```

Either way you are looking at the e2e project's flock, not your real queue.

Projects live in the database, not on disk, so `e2e-env.sh` cannot create
them as part of its build step — `e2e-daemon.sh start` runs
`e2e-env.sh --register` once the API answers instead. That matters most for
Tier 2, which runs no smoke and would otherwise find an empty daemon. It is
idempotent, so running it again is free.

`scripts/e2e/aq.py` exists because neither obvious way to invoke the CLI is
safe here. The installed `aq` console script resolves `src` through the
editable install, which may be a different checkout than the worktree under
test. And `python3 -m src.cli.app` loads `src/cli/app.py` *twice* — once as
`__main__`, again as `src.cli.app` when `from .app import cli` runs inside
`src/cli/doctor.py` and friends — so every hand-written group (`doctor`,
`session`, `formula`, …) registers on the other module's `cli` object and
disappears from `--help`. The launcher puts the repo root on `sys.path` and
imports `src.cli.app` exactly once under its real name.

### The session token

With `sessions.provider: fake` nothing is spawned, so the runner has to *be*
the pool worker. `aq session token <session-id>` mints a fresh bearer token
for an existing session; the runner then sets `AQ_API_TOKEN` and
`AQ_SESSION_ID` and runs ordinary `aq` commands. That is the same environment
handshake `src/sessions/env.py` performs inside a real session, so what the
daemon sees is indistinguishable from a live worker.

`session_token` is a **dev/e2e facility**. It is deliberately kept out of
`AGENT_COMMAND_SET` (an agent's own token cannot mint another session's), and
excluded from MCP entirely — only a loopback CLI caller or an elevated
supervisor token can reach it.

### What each scenario proves

**S1 — pool sizing.** Three READY tasks routed to the `worker` pool profile.
Within a few 5s cascades `aq pool status` reports supply of exactly two and
`aq session list --lifecycle pool` has exactly two live session rows, bounded
by `max_active`. The wait includes
`aq system get-recent-events --event-type pool.scaled` carrying the scale-up
audit row: `starting` includes reservations before session rows exist, and
the audit follows completion of the launch batch. Every sampled supply and
session count must stay at or below two, including during startup.
*Regression it catches: a sizer that ignores its bounds, or
one that never fires at all.*

**S2 — the claim loop.** The whole worker lifecycle through one session's own
token: `task claim --next` returns `claimed` with a `claim_epoch`;
`task heartbeat` with a wrong epoch is refused as `stale_claim` and with the
right one is accepted; `task close --claim-next` closes and immediately
answers `drain_requested` because `swarm.fresh_context_per_task` limits the
session to one claim; `aq session drain-ack` retires the worker and the sizer
starts a replacement for the still-unclaimed work. *Regression it catches:
the epoch fence not fencing, `--claim-next` losing its scope, or a fresh-context
worker stranding its workspace.*

`agents.state == RETIRED` has no public reader (`aq agent list` reports
*workspace slots*, not agent rows), so S2 asserts the three consequences that
are observable: the session row goes terminal, `pools.orphan_agents` stays
clean — that check *does* read agent rows and would flag one left behind —
and a replacement session appears.

**S3 — worker-filed work.** A worker holding a task files another, as a root
filing. It lands with a DEFINED creation result, pinned to the session's
project, and carries no profile and no class: filing takes hints only and the
filer's own route never carries over, so the filing is `unrouted` like any new
task. Its `discovered-from` edge and open `routing` gate keep `is_blocked` true;
with this fixture's non-authoritative blocked-state projection, its status may
promote to READY before the next read. `aq task route-override` — standing in
for the router, which Tier 1 runs without an LLM, and the only other writer
that resolves a routing gate — then writes the profile and class and resolves
the gate; `aq task explain` stops reporting the task as gate-blocked.
*Regression it catches: filings escaping their project, arriving unrouted-but-
runnable, or losing their provenance.*

**S4 — formulas.** `aq formula list` sees both fixtures; `aq formula show
review-and-fix --var branch=feat/x` resolves the `extends` chain
(`base-review` → `review-and-fix`) and substitutes vars in every node title;
`aq formula cook` writes the container + two children with a
`formula:review-and-fix` label; `aq formula show --as-cooked <container>`
renders back the snapshot the cook actually wrote and it matches; both
children are closed through their own sessions — with `--work-outcome no-op`,
because the runner commits nothing and a `shipped` close under the
`pull_request` default is refused for a PR the bare e2e remote can never
carry — and the container settles to COMPLETED. *Regression it catches: a chain that resolves differently than it
cooks, a snapshot that drifts from the graph, a container that never settles.*

**S5 — fence and scope.** A second pool session's token cannot heartbeat the
first session's task (`out_of_scope`), and a token scoped to `e2e` cannot
`aq prime` a task in project `other`. Before those assertions, the holder
retries only `no_ready_work` within the convergence budget and must claim the
exact fixture: even a routed `READY` row can be skipped while another transaction
locks it. A timeout reports the last claim and fixture state; other claim
failures stop immediately. *Regression it catches: a token being
treated as a key to the daemon rather than an identity.*

**S6 — doctor.** Every `pools.*`, `claims.*`, `hierarchy.*` and
`formulas.parse` check is clean. Then `swarm.enabled` is flipped to false
through `aq system update-config` (hot-reload, no restart), `pools.disabled`
warns, and flipping back restores it. *Regression it catches: a health check
that cannot see the flag it reports on, and a hot-reload that does not
reload.*

**S7 — the PostgreSQL race.** Two workers claim `--next` concurrently, as two
real processes, against one READY task. Exactly one gets `claimed`; the other
gets `no_ready_work` or `claim_conflict`. *Regression it catches: the
`FOR UPDATE SKIP LOCKED` work query losing its exclusivity, which only two
real processes against one real database can demonstrate.*

**S8 — project onboarding.** The real `aq project onboard` CLI links a seeded
repository beneath the configured disposable root and initializes a second
repository with the default `main` branch and README commit. CLI reads then
verify that each operation created exactly one project and one enabled primary
`project-repo` workspace; filesystem checks verify the linked repository stayed
byte-for-byte clean and both standard vault trees exist. *Regression it catches:
the public CLI, configured-root authorization, saga registration, Git setup, and
vault setup working separately but failing when composed through a real daemon.*

**S9 — task lifecycle.** A partial noninteractive `aq --json task create`
invocation must exit 2, name its missing flag, and leave no task behind; the
full noninteractive flag set, including a workspace requirement, then creates
a real task. Its id comes from the JSON receipt, not terminal scraping.
Separate reads prove persistence; `task set` proves update semantics; invalid
priority proves nonzero exit plus rollback; deletion proves the final edge.

**S10 — workspace, file, git and notes.** A clone reserved by `e2e-env.sh` is
registered and removed through the workspace CLI. Duplicate registration and
an out-of-workspace file write are refused without residue. File write/read/edit,
git branch/commit/push/log, and note write/append/read/delete are all exercised
against disposable disk and a bare local remote.

**S11 — messages.** The durable queue sends, reads, injects and replies to a
synthetic `profile:e2e-sink-*` mailbox. Profile mailboxes are pull-only, and
`messaging_platform: none` is a second hard network fence: no Discord, webhook
or human receives anything. A malformed recipient verifies the CLI's nonzero
failure contract.

**S12 — MCP registry.** Project-scoped create/read/delete goes through the
daemon and vault watcher. The only probe targets `127.0.0.1:1`, deliberately
proving `dependency-unavailable` reporting without contacting a real MCP
service.

**S13 — plugin extensions.** `e2e-env.sh` creates a tiny distribution metadata
fixture without installing it. One subprocess opts in with `PYTHONPATH` and
runs its command; another omits the path and must receive Click's exit-2
unknown-command response. No package manager or interpreter environment is
modified.

**S14 — graph and vault.** Layout rebuild and tidy run through the daemon,
including a missing-project refusal. Vault migration is invoked only with
`--dry-run --data-dir "$AQ_E2E_HOME"`; database upgrade and operator-daemon
control remain explicitly untested.

**S15 — retired.** The old development integration scenario depended on the
removed adoption, provenance migration and autonomous sweep commands. It was
retired with that engine on 2026-10-04. Current root delivery is exercised by the
reconciler Git scenarios in `tests/test_integration_root_scenarios.py`; the
remaining swarm scenarios retain their original numbers.

**S16 — provider failover.** The end-to-end check of
[provider failover](../specs/provider-failover.md) (D23), against the fake
provider kit below. It queues six router-routed (`class_only`) tasks on
`prova` — five at `std-high`, one at `solo-high` — and one pinned there with
`aq task route-override`, then logs `prova` out.
It asserts that `prova` turns `unauthenticated` within two launches and that
nothing launches against it afterwards (counted from the provider's own
`startup_dialog` evidence, because a startup death leaves no session row);
that `aq provider held-tasks` and `aq task explain` name every hold
(`provider_pinned`, `no_equivalent_rung`, and `failover_inactive` for the rest,
because S16 temporarily pauses the automatic provider-failover playbook while
it exercises the same policy through the operator commands); and that `aq provider reroute --dry-run`
plans exactly what the live sweep then does. The live sweep is the command the
`provider-failover` playbook calls. The sweep moves one task at a time into
`provb`'s `max_active: 1` — the runner works it as the `provb` session, and
the top-up sweep joins the same batch — and `provb` never runs two sessions.
It then checks one outage notice to `user:dashboard`, one batch notice to
`supervisor-e2e`, one `high` escalation, and the status counts. Recovery comes
next: the script restores `prova`, `aq provider recheck` puts it on probation,
the single canary launch claims a held task and turns `prova` `available`, the
pinned task runs on `prova`, the moved-and-queued task stays on `provb` until
`aq provider reroute-undo` returns it, and the escalation resolves. Finally
every provider goes down (`prova` logged out, the others disabled by override):
the sweep moves nothing and holds everything as `all_providers_unavailable`, a
live claude session's claim answers `drain_requested`, the escalation turns
`critical`, and `providers.availability` reports `error`. Cleanup restores
every provider whatever happened. *Regression it catches: a provider outage
that keeps launching, work that moves past a pool's bound or across a class, a
pin that moves, a hold with no reason, or a recovery that never completes.*

**S17 — phased graph.** A disposable development-mode project is onboarded
through the real CLI, then a vault spec with two declared phases, three nodes,
and a graph-seeded three-row checklist is cooked through
`aq task create --from-spec`. Its dry run reports both phases and all three
checklist rows without persistence. The runner verifies phase 2 and its node
are withheld, claims both phase-1 nodes as pool workers, and reads the
checklist from the real `aq prime` output. A close without
`--skip-open-subtasks` must refuse with `subtasks.open`; the override then
closes the task and leaves every row `skipped` with the `skipped at close`
note. Once the branchless phase-1 container settles `COMPLETED`, phase 2
unblocks and a replacement worker can claim it — no development publication
sweep occurs between those observations. The scenario closes and deletes the
epic so it is re-runnable. *Regression it catches: a graph writer that loses
phase/checklist data, a later phase escaping its parent gate, development
delivery accidentally treating a phase container as a branch owner, or a
checklist bypassing the close fence.*

**S18 — supervisor failure triage.** Tier 1 enables only the deterministic
command-bearing system playbooks; it still starts no model process or
supervisor session. The scenario gives a disposable pool worker one explicit
assignment, closes it with a hard failure, then verifies the resulting
`task.failed` event completed the reviewed `supervisor-failure-triage` run for
that task and created exactly one durable notice for `supervisor-e2e`. It
deletes the terminal fixture afterward. *Regression it catches: a reviewed
failure-triage bundle that is not activated, does not receive terminal worker
events, calls the wrong command, or fails to persist its supervisor incident.*

### The fake provider kit

`tests/fixtures/provider_failover/` holds everything S16 needs, and
`e2e-env.sh` copies it into the vault:

| File | What it is |
|---|---|
| `harnesses/prova.md`, `harnesses/provb.md` | Two vendorless fake harnesses, each declaring a `login-required` (`signal: auth`) and a `usage-limit` (`signal: usage`) quarantine dialog. Their provider keys are their ids. |
| `intelligence-classes/std-high.md` | A class with a slice for both harness ids, so `std-high-prova` and `std-high-provb` are equivalent rungs. |
| `intelligence-classes/solo-high.md` | A class with a `prova` slice only — the `astra-high` analogue. A task on it holds with `no_equivalent_rung`. |
| `agent-types/std-high-prova`, `std-high-provb`, `solo-high-prova` | Hand-authored pool profiles, `max_active: 1`. `WORKER_PROVIDERS` is fixed, so the kit authors rungs rather than deriving them; this also proves that an operator-authored profile fails over. |

Nothing ever runs a `prova` binary. `sessions.fake_script_file` (Tier 1 only;
`$AQ_E2E_HOME/fake-provider-script.json`) maps each fake harness to a mode, and
the fake session provider re-reads it on every start
([`src/sessions/fake_script.py`](../../src/sessions/fake_script.py)):

| Mode | A start… |
|---|---|
| `ok` | succeeds (also the default for a harness the file does not name) |
| `login_required` | dies on the `login-required` dialog — a logged-out CLI |
| `usage_limit` | dies on the `usage-limit` dialog — an exhausted account |
| `crash` | dies with no dialog — an unattributed startup death |
| `rate_limit_midtask` | succeeds, then exits `after_s` seconds later with a usage-limit line in its pane |

The same file answers the daemon's login probe for the harnesses it names
(`login_required` is *not signed in*, anything else *signed in*), and a
fake-session daemon with a script probes no real CLI at all. So you can drive
an outage by hand against a running kit:

```bash
echo '{"prova": "login_required", "provb": "ok"}' > "$AQ_E2E_HOME/fake-provider-script.json"
# queue work on std-high-prova, then:
aq provider status --provider prova
aq provider reroute --dry-run
echo '{"prova": "ok", "provb": "ok"}' > "$AQ_E2E_HOME/fake-provider-script.json"
aq provider recheck --provider prova
```

Keep S16's cleanup order in mind if you do: `aq provider set-state … --state auto`
puts a still-unavailable provider on probation. Probation admits one canary
launch, and a canary nobody works holds that provider's launches for
`CANARY_TIMEOUT_SECONDS` (10 minutes). So clear the queued work first.

### Readiness is not liveness

`GET /api/health` is a static stub: it answers `{"status": "ok"}` as soon as
uvicorn is listening and never touches the database. `GET /ready` runs the
daemon's own health provider, so its `checks.database` entry is a real query
through the engine the daemon is using.

The kit gates on `/ready`, through `scripts/e2e/probe.py`:

* `e2e-daemon.sh start` waits for *ready*, not for a listening port, and fails
  — printing the database error and the log tail — if readiness never arrives;
* `e2e-daemon.sh status` exits non-zero when the daemon is answering but cannot
  reach its schema, which is what stops `e2e-smoke.sh` from reusing it;
* `e2e-smoke.sh` re-probes immediately before the first scenario.

This exists because the failure it catches is invisible otherwise. A daemon
whose schema setup died, or whose database was dropped out from under it, keeps
serving `/api/health`; the eighteen scenarios then run against an empty database
and every one fails with `relation "projects" does not exist`, which reads like
eighteen product regressions rather than one broken environment.

### Running two kits at once

`AQ_E2E_HOME`, `AQ_E2E_PORT` and `E2E_DB_NAME` default to per-box values, not
per-worktree ones, so two checkouts running the kit share one home, one port
and one database unless you say otherwise:

```bash
export AQ_E2E_HOME=~/.agent-queue-e2e-$(basename "$PWD") \
       AQ_E2E_PORT=8123 \
       E2E_DB_NAME=agent_queue_e2e_myslot
scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh
```

Without that, one checkout's `e2e-env.sh --reset` terminates the other's
database connections, drops its database and deletes its home — including the
log file, which the running daemon keeps writing to an unlinked inode, so
`e2e-daemon.sh logs` shows nothing.

`--reset` refuses to do this to a daemon it does not own: if something is
answering on `$AQ_E2E_API_URL` and `$AQ_E2E_HOME/daemon.pid` does not name a
live process, it exits 2 and points here. When the pid file *does* name one,
the reset stops it first rather than orphaning it.

### Reading a failure

Every scenario prints its own reason on the line under `FAIL`; the waits name
what they were waiting for and what they last saw, so start there. Then:

```bash
scripts/e2e-daemon.sh logs 200                      # the daemon's own account
AQ_API_URL=http://127.0.0.1:8099 aq doctor           # what the system thinks of itself
AQ_API_URL=http://127.0.0.1:8099 aq system get-recent-events --limit 40
AQ_API_URL=http://127.0.0.1:8099 aq pool status
AQ_API_URL=http://127.0.0.1:8099 aq session list
```

(The scripts set `AQ_API_URL` for you; you need it only when running `aq` by
hand.) Re-run one scenario with `scripts/e2e-smoke.sh S3` against a daemon
you started yourself, so the state that failed is still there to look at.

A scenario that fails *after* an earlier one left the pool in an odd shape is
not usually a real failure — S5 and S7 rebuild the pool from scratch for
exactly that reason. If S1–S4 pass and S5+ fail, suspect the rebuild before
suspecting the claim protocol.

### Known surface gaps the kit works around

These are not bugs the kit hides; they are places where the CLI cannot yet
express what the runner needs, and it falls back to `POST /api/execute` —
just as public a surface.

- S1–S7 retain a small REST helper for fixture setup and commands whose
  generated Click schema cannot carry their arguments. S9 is the acceptance
  path for task creation itself: `aq --json task create ...` returns the new
  id at `.data.created` (design §4.2.1), and the runner consumes that receipt.
- `gate_list` / `explain_task` / `list_agents` carry codegen-only input
  schemas, so their auto-generated Click commands take no options.
- `agents.state` has no public reader at all — see S2 above.

## Tier 2 — with a real harness

Same environment, one switch. The daemon then spawns actual `claude`
processes in tmux and the *agent* runs the claim loop instead of the script.

```bash
scripts/e2e-daemon.sh stop
AQ_E2E_SESSION_PROVIDER=tmux scripts/e2e-env.sh     # rewrites config.yaml only
scripts/e2e-daemon.sh start
```

Run Tier 2 in its **own** home so it cannot collide with a Tier 1 run:

```bash
export AQ_E2E_HOME=~/.agent-queue-e2e-live AQ_E2E_PORT=8098 \
       E2E_DB_NAME=agent_queue_e2e_live AQ_E2E_SESSION_PROVIDER=tmux
scripts/e2e-env.sh --reset && scripts/e2e-daemon.sh start
```

That switch does more than change the provider. Tier 1 already runs the
deterministic command-only playbooks for S18, but a live agent needs the
following additional subsystems, which `e2e-env.sh` turns on when the provider
is not `fake`:

- `messages.enabled` + `supervisor_agent.enabled` — the per-project
  supervisor sessions the dashboard's chat talks to.

Separately — and for *both* tiers, since it costs nothing when no dialog
appears — the generated config sets `sessions.dialog_budget_seconds: 45`.
Only Tier 2 can ever hit it; see below.

Then create the demand by hand and watch. Filing takes hints only, never a
profile: give each task a kind and the `worker` pool's class, `fast-high`, and
the project's router routes it (both hints given, so no classification call).
`aq task show <id>` names the route, and `aq task explain` says why a task is
still unrouted:

```bash
export AQ_API_URL=http://127.0.0.1:8099
aq task create -p e2e -t "Fix the failing test in tests/test_math.py" --type bugfix --intelligence-class fast-high
aq task create -p e2e -t "Add a docstring to e2e_pkg.add" --type docs --intelligence-class fast-high
aq task create -p e2e -t "Note the package layout in README.md" --type docs --intelligence-class fast-high

aq pool status
aq session list --lifecycle pool
tmux -L aq-e2e attach                # ctrl-b d to detach without killing it
```

S1, S2 and S4 are the three worth watching live; S3/S5/S6/S7/S8 are protocol
and operator-surface assertions that Tier 1 already covers deterministically.

What to watch for, in order:

1. **The bootstrap prompt.** The pool session opens with the claim-loop
   prompt, not a task prompt — a pool worker has no task at launch. Built by
   `SessionSpecBuilder.build_pool_spec`.
2. **`aq prime`.** The agent's first real call. It should print the task it
   just claimed, with no `--task-id` flag anywhere: the token defines the
   identity.
3. **`.aq/claim.json`** in the worker's workspace (`~/.agent-queue-e2e/
   workspaces/e2e-N/.aq/claim.json`). It carries `task_id`, `claim_epoch`,
   `session_id`. This is where the agent reads the epoch it must pass to
   `heartbeat` and `close`; if it is stale or missing, every fenced call is
   refused and that is the first thing to check.
4. **`close --claim-next`.** The loop's hinge. One call closes and claims
   again when context reuse is enabled. In this fixture, the default
   `swarm.fresh_context_per_task: true` instead returns `drain_requested`.
5. **`drain_requested` → `aq session drain-ack`.** After one claim the
   conversation is spent. It should ack, the pane should die, and a *new*
   pool session should appear for the next task with a fresh context.

Failure modes that only show up here: a bootstrap prompt the model
misreads (it asks a question instead of claiming), a harness that swallows
the `--claim-epoch` flag, and a worker that closes without `--claim-next`
and then sits idle holding a workspace.

### Two things that will bite you first

**`aq` inside the session must be *this* worktree's.** The pip-installed
`aq` resolves `src` through the editable install — usually a different
checkout, with no `aq task claim` in it at all — so a worker fails its very
first command with "No such command". `e2e-env.sh` writes
`$AQ_E2E_HOME/bin/aq` (a wrapper around `scripts/e2e/aq.py`) and
`e2e-daemon.sh start` puts that directory first on the daemon's PATH, which
tmux sessions inherit. Verify before blaming the agent:

```bash
tmux -L aq-e2e list-sessions
tmux -L aq-e2e show-environment -t <session> PATH
# or, inside a pane:
which aq && aq pool status
```

If a tmux server was already running on the `aq-e2e` socket before the
daemon started, it kept its *own* environment and never saw the new PATH —
`tmux -L aq-e2e kill-server` and restart the daemon.

**The trust dialog.** `claude`'s first run in a directory it has not seen
draws a "do you trust the files in this folder?" prompt. On a cold cache it
can take well over ten seconds to appear — past the stock
`dialog_budget_seconds: 8`, so the harness's auto-dismiss has already given
up and the session parks on the dialog until a human presses Enter. The
symptom is a session that is "running" with no output and no claim, and a
supervisor that appears to hang. `e2e-env.sh` writes 45s into every config
it generates (harmless under Tier 1, which spawns nothing); if you still
catch one, attach and press Enter once — the answer is remembered per
directory, so it only happens on a fresh workspace.

### The supervisor chat

With `supervisor_agent.enabled` (Tier 2 sets it), `scripts/e2e-dashboard.sh`
gives you a chat box. It addresses `supervisor-<project>` — which cold-starts
a `claude` session for that project on first message — or `supervisor-global`.
Those sessions appear in `aq session list` with `lifecycle: named`, alongside
the pool workers, and are the same thing the real deployment's Discord chat
talks to.

### Teardown

```bash
scripts/e2e-clean.sh
```

The cleanup command refuses broad/protected paths, any directory without
the `.aq-e2e` ownership marker, and the operator/default tmux sockets (`aq`
and `default`) before it stops a daemon or invokes any destructive command.
It then drops only `E2E_DB_NAME`. The daemon's
Tier-2 sessions use the `aq-e2e` tmux socket; cleanup of the marked e2e home
and daemon cannot target the operator's default data directory or database.

### Automated gates

Fast fixture/contract checks use the normal default marker set:

```bash
POSTGRES_TEST_DSN=<admin-dsn> aq test \
  tests/test_e2e_kit_fixtures.py tests/test_script_modes.py -q
```

The real daemon/PostgreSQL run is explicitly marked `integration`, so a normal
`aq test` never starts it. Run it deliberately through the global test gate:

```bash
POSTGRES_TEST_DSN=<admin-dsn> aq test \
  tests/test_e2e_cli_stateful.py -m integration -n auto --dist loadgroup -q
```

The test chooses a unique database, port and temporary home, uses fake
sessions, and calls `e2e-clean.sh` in `finally`. Tmux/real-provider testing
remains Tier 2 and is never part of the default suite.

## Extending the kit

Add a scenario as a function in `scripts/e2e/smoke.py` taking the shared
`state` dict and returning a one-line summary string; register it in
`SCENARIOS`. Raise `Failure("…")` (or use `check(cond, "…")`) for an
assertion — the message is what the operator reads at 3am, so name what was
expected and what was actually there. Use `wait_for(pred, what="…")` rather
than `sleep`; every wait is on the 5s cascade and its `what` becomes the
timeout message.

Assertions must go through a public surface — `aq …` with `--json`, or
`POST /api/execute`. Reading the database directly would let the kit pass
while the surface an agent actually uses is broken, which is the whole thing
it exists to prevent.

## Running the kit with the git-first protocol active

The cutover smoke runs the same kit with `integration.git_first: active`, so the
reduced train — not the subject runtime — is the delivery path the daemon
builds. `AQ_E2E_EXTRA_CONFIG` appends a caller-owned YAML fragment verbatim, so
no script needs to know about the selector:

```bash
printf 'integration:\n  git_first: active\n' > /tmp/aq-e2e-git-first.yaml
AQ_E2E_EXTRA_CONFIG=/tmp/aq-e2e-git-first.yaml scripts/e2e-env.sh --reset
scripts/e2e-smoke.sh
```

`--reset` drops the database, so it cannot be combined with `--register`;
`scripts/e2e-daemon.sh start` registers the projects for you. The daemon must
report `projection_kind: train` from `aq integration status`; if it does not,
the selector did not take effect and the run proves nothing about the cutover.

This run is a prerequisite of the operator canary in
[the git-first cutover runbook](git-first-cutover-runbook.md), together with
`aq test tests/test_integration*.py tests/test_config.py`. Both must be green
before `active` is selected on a real install. A development-mode delivery here
does not prove hosted-train operation, and vice versa: the project's actual
configured mode is proven only by the live canary.

`S20` closes an ordinary root task in `train` mode against a disposable bare
repository, verifies its immutable completion provenance, and under `active`
waits for the train to publish the exact source and file onto `main`. It pins
local validation before selecting train mode, so it needs neither hosted CI nor
an LLM. Its canonical `aq/epic/` source changes a path whose earlier target diff
exceeds the 1 MiB command stdin limit, exercising whole-source patch streaming
and rejecting unknown-delivery blockers before publication. The fake daemon
substitutes GitHub credentials for remotes inside its
marked disposable home; candidate construction, fenced pushes and Git delivery
remain real. With `shadow`, it checks completion provenance and explicitly reports
delivery as untested. The CI `cli` shard selects `active` to run the full proof.

## The App-mode train proof

`scripts/e2e-app-train.sh` builds on this kit's isolation to run the App-mode
spec's live proof (§10, S1-S10) against a disposable GitHub repository. It
adds four things:
- its own PostgreSQL container;
- the `integration.github_app` block, appended through `AQ_E2E_EXTRA_CONFIG`;
- pool profiles for the train;
- a daemon started with an empty `GH_CONFIG_DIR` and no GitHub token.

`scripts/e2e/app_train.py` holds one resumable step per scenario. Every step
from `s1` on mutates the repository and refuses to run until the operator's
approval is written to `$AQ_E2E_HOME/APPROVED`. The first run is recorded in
[app-mode-train-2026-09-28.md](../gates/app-mode-train-2026-09-28.md).

The `hotfix` step exercises a prepared one-step `dev -> main` promotion flow
with `after.backmerge: true`. It files and routes a bugfix off `main`, completes
the fixture worker, opens the exact-head promotion PR onto `main`, applies the
configured human approval, then authors the backmerge's own PR onto `dev` and
waits for Git containment. Versioned flows need the PATCH version and notes in
the supplied file changes, for example:

```bash
bash scripts/e2e-app-train.sh run hotfix --scenario hotfix-main-dev \
  --version 0.0.2 --copy pyproject.toml=/tmp/fixture-pyproject.toml \
  --copy notes/0.0.2.md=/tmp/fixture-notes.md
```

The paths must match the prepared fixture's version and notes configuration.
The step records resumable evidence in the fixture home and requires its
existing `APPROVED` file. The isolated PostgreSQL/Git proof also runs in
`tests/test_promotion_backmerge.py`, including both integration modes, a held
lower-branch PR, a new frozen batch, and delivery of the hotfix source to `dev`.
