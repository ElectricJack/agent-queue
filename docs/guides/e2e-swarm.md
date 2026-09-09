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
| Playbooks / supervisor | off | on (a live agent needs them) |
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
pip install -e ".[dev,cli]"            # and packages/aq-client
git --version                          # any recent git
```

Tier 2 additionally needs `tmux` and a `claude` binary on `PATH`.

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

The default run executes all 14 scenarios. `S1`–`S8` cover swarm composition;
`S9`–`S14` cover the wider stateful CLI surface. Every CLI subprocess is
forced back to this disposable data directory and database even when the
caller is a worker carrying production-refusal sentinels.

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

14/14 scenarios passed
```

The runner exits non-zero if any scenario fails. It then prints a capability
report whose statuses are deliberately finite: `passed`, `broken`,
`unsupported`, `dependency-unavailable`, and `explicitly-untested`. This keeps
an absent optional service or retired surface from being reported as either a
false pass or a product regression.

Tier 1 keeps assignment deterministic without an LLM: its generated execution
profiles carry fixed `default_class` values, and the runner and formula fixtures
write those intelligence classes explicitly onto tasks. This mirrors a completed
assignment-routing decision and makes pool claims exercise the live session's
class and model constraints.

### What the pieces are

| File | What it does |
|---|---|
| `scripts/e2e-common.sh` | shared paths, ports and DSNs; every value overridable from the environment |
| `scripts/e2e-env.sh` | builds the world: dirs, bare git repos + workspace clones, an onboarding root, an opt-in plugin entry point, vault fixtures, `bin/aq`, config, and database |
| `scripts/e2e-daemon.sh` | `start` / `stop` / `status` / `logs` for the isolated daemon |
| `scripts/e2e-clean.sh` | validates path ownership and the isolated tmux socket before any side effect, then stops the disposable daemon, drops only its database, and removes only its data directory |
| `scripts/e2e-smoke.sh` | the Tier 1 runner (thin wrapper) |
| `scripts/e2e/smoke.py` | the 14 scenarios and capability report |
| `scripts/e2e/aq.py` | runs *this worktree's* `aq` — see below |
| `scripts/e2e/register.py` | creates the `e2e` / `other` projects + their workspaces (needs the daemon) |
| `scripts/e2e/dbsetup.py` | creates/drops `agent_queue_e2e` via asyncpg (no `psql` needed) |
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
Within a few 5s cascades `aq pool status` shows exactly two live sessions —
`max_active`, not "one per task" — and
`aq system get-recent-events --event-type pool.scaled` carries the audit row
for the scale-up. *Regression it catches: a sizer that ignores its bounds, or
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

**S3 — worker-filed work.** A worker holding a task files another. It lands
with a DEFINED creation result, pinned to the session's project, and inherits
the caller's profile as its capability ceiling. Its `discovered-from` edge and
open `routing` gate keep `is_blocked` true; with this fixture's non-authoritative
blocked-state projection, its status may promote to READY before the next read.
`aq task route` — the only resolver for a routing gate, and what a triage agent
would call — then writes the explicit intelligence class and resolves the gate;
`aq task explain` stops reporting the task as gate-blocked.
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
`aq prime` a task in project `other`. *Regression it catches: a token being
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
`FOR UPDATE SKIP LOCKED` work query losing its exclusivity — the failure that
unit tests on SQLite cannot see.*

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

That switch does more than change the provider. A live agent needs three
subsystems Tier 1 deliberately runs without, and `e2e-env.sh` turns all
three on when the provider is not `fake`:

- `playbooks.enabled` — the default pipeline's worker-filed triage is what
  routes a task an agent files. Without it a filing sits DEFINED behind its
  routing gate forever (which is exactly what S3 asserts, and exactly what
  you do *not* want when watching a live run).
- `messages.enabled` + `supervisor_agent.enabled` — the per-project
  supervisor sessions the dashboard's chat talks to.

Separately — and for *both* tiers, since it costs nothing when no dialog
appears — the generated config sets `sessions.dialog_budget_seconds: 45`.
Only Tier 2 can ever hit it; see below.

Then create the demand by hand and watch:

```bash
export AQ_API_URL=http://127.0.0.1:8099
aq task create -p e2e -t "Fix the failing test in tests/test_math.py" -P worker
aq task create -p e2e -t "Add a docstring to e2e_pkg.add" -P worker
aq task create -p e2e -t "Note the package layout in README.md" -P worker

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
  tests/test_e2e_cli_stateful.py -m integration -q
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
