# Deploying Agent Queue

A containerised deployment of the full stack — daemon, dashboard, and a managed
PostgreSQL — that runs on any Docker host: a cloud VM, or your own machine.

This directory is the single home for deployment: the images, the compose stack,
the configuration, and the reasoning. This page is the task guide — how to run
it. [`DESIGN.md`](DESIGN.md) is why it is shaped this way, what constrains it,
and what is planned next.

## Quick start

### Entirely local, including the database

Nothing external required — the `local-db` profile brings its own PostgreSQL,
on the compose network only, so it cannot collide with other databases you have
running:

```bash
cd deploy
cp .env.example .env
# in .env:
#   AQ_DATABASE_URL=postgresql+asyncpg://agent_queue:agent_queue_dev@postgres:5432/agent_queue
docker compose -f docker-compose.prod.yml --profile local-db up -d --build
open http://127.0.0.1:8088
```

### Against an existing PostgreSQL

Managed (Cloud SQL private IP, RDS, Neon) — just set the DSN:

```bash
cp .env.example .env && $EDITOR .env          # AQ_DATABASE_URL is the only required var
docker compose -f docker-compose.prod.yml up -d --build
ssh -L 8088:127.0.0.1:8088 <host>             # then open http://127.0.0.1:8088
```

To use a PostgreSQL running on the Docker **host** instead, uncomment the
`extra_hosts` block on the `daemon` service and point the DSN at
`host.docker.internal` — e.g. the repo's dev database from the root
`docker-compose.yml`:

```
AQ_DATABASE_URL=postgresql+asyncpg://agent_queue:agent_queue_dev@host.docker.internal:5533/agent_queue
```

### Ports

The dashboard publishes to `127.0.0.1:8088` by default, not 8080, which is
commonly taken. Override with `AQ_DASHBOARD_PORT`. The daemon publishes nothing.

## Isolation from a native install

This stack is a **separate AQ instance**. It is designed to sit alongside a
normal development install without touching it, and every boundary below is
enforced by configuration in this directory — nothing is shared implicitly.

| | Native install | This stack |
|---|---|---|
| Compose project | `agent-queue2` (the repo's dev PostgreSQL) | `agent-queue-deploy` |
| Database | yours, e.g. `:5533` on the host | its own, on the compose network, **no host port** |
| Data dir / vault | `~/.agent-queue` | volume `…_aq-data` → `/data/agent-queue` |
| Workspaces | your own checkouts | volume `…_aq-workspaces` → `/data/workspaces` |
| Harness login + transcripts | `~/.claude` | volume `…_aq-home` → `/home/aq/.claude` |
| API port | `8081` on the host | `8081` **inside** the container, not published |
| Dashboard | yours | `127.0.0.1:8088` |
| tmux sessions | your host tmux socket | the container's own filesystem |

The compose project is named `agent-queue-deploy` rather than `agent-queue`
deliberately: `agent-queue` is the repository directory name, which is the
*default* project name Docker derives for any unnamed compose file run from this
repo. Sharing that namespace would let an unrelated `docker compose down` stop
these containers.

**Two rules keep it that way.**

1. **Never point this stack and a native daemon at the same database.** AQ's
   scheduler is machine-scoped — one daemon per database. Two daemons converging
   the same fleet-wide pool bounds will double-provision workers, and neither can
   see the other's tmux sessions. See [`DESIGN.md` §3](DESIGN.md#3-the-hard-constraint-the-scheduler-is-machine-scoped).
2. **Think before bind-mounting host paths.** Mounting your `~/.claude` shares
   one OAuth credential and exposes your personal Claude Code transcripts to the
   daemon, which reads `~/.claude/projects/` as agent output. Mounting a host
   repository puts container agents in the same working tree you are editing.
   Both are occasionally what you want; neither is the default here.

To authenticate the container without sharing your host login, log in inside it:

```bash
docker compose -f docker-compose.prod.yml exec daemon claude auth login
```

The credential lands in the `…_aq-home` volume and survives rebuilds.

## Deploying to GCP

`terraform/gcp/` provisions step 1 of [`DESIGN.md` §8](DESIGN.md#8-the-committed-target):
one ordinary VM running this compose stack, a persistent data disk that outlives
it, and Cloud SQL on a private IP.

```bash
cd deploy/terraform/gcp
cp terraform.tfvars.example terraform.tfvars   # project_id is the only required value
terraform init
terraform plan
terraform apply
```

Then, from the outputs:

```bash
$(terraform output -raw ssh_command)                 # IAP SSH; there is no external IP
$(terraform output -raw dashboard_tunnel_command)    # then open http://127.0.0.1:8088
```

Finally, log the harnesses in on the VM — once each, persisted on their own
volumes:

```bash
cd /opt/aq/agent-queue/deploy
sudo docker compose -f docker-compose.prod.yml exec daemon claude auth login
sudo docker compose -f docker-compose.prod.yml exec daemon codex login --device-auth
```

### What it builds, and the three decisions inside it

- **No external IP.** Egress is Cloud NAT (agents must reach LLM APIs); ingress
  is a single firewall rule for IAP's `35.235.240.0/20`. Given the web layer has
  effectively no authentication, the network *is* the security model — see
  [Security posture](#security-posture).
- **The data disk is separate and `prevent_destroy`.** It holds the vault, the
  base clones and every worktree slot, and PostgreSQL rows reference those
  paths. Docker's `data-root` is moved onto it so named volumes live there
  rather than on the boot disk. `data_disk_type` rejects Local SSD, which is
  wiped on preemption.
- **`provisioning_model` is a variable, defaulted to `STANDARD`.** Flipping to
  `SPOT` later is that one value — same disk, same image, same DSN. Read
  [`DESIGN.md` §6.3](DESIGN.md#63-two-failure-modes-to-design-around) first:
  something must restart the daemon after a preemption, because workspace locks
  are released *by the daemon restarting*.

`cloud-init.yaml` is idempotent and re-runs on every boot, so a preempted
instance brings the stack back up by itself. It formats the data disk **only**
when it has no filesystem — getting that wrong on a restart would erase every
worktree slot.

### Before you apply

- **Terraform state contains the generated database password in plaintext.** It
  is gitignored. For anything beyond a single operator, move to a GCS backend
  with versioning and restricted IAM before the first `apply`.
- `terraform validate` passes and the config is `fmt`-clean, but it has never
  been applied to a real project. Validation checks syntax and schema, not
  whether the APIs, quotas and IAM in *your* project cooperate — treat the first
  `plan` as the review step.
- `db_version` defaults to `POSTGRES_17`; the local compose stack uses
  PostgreSQL 18. Neither needs server-side extensions, so the version is a free
  choice — align them if you care.

## Development workflow

**This stack is not a development loop.** It is the deployment artifact. Keep
developing against a native install and use these images to answer a different
question: *does this still work when packaged?*

| | Use |
|---|---|
| Features, fixes, tests | Native install. Edit, restart the daemon, done. |
| Packaging, config shape, dependencies, harness CLIs | This stack, before a deploy. |
| Cloud | This stack on a VM, pointed at managed SQL. |

The daemon image installs the project editable, so **any `src/` edit invalidates
the pip layer and costs a full rebuild**. That is the right trade for a
deployment image — one build per release — and a poor one for iteration. There is
no reason to accept a two-minute inner loop when the native install gives you an
instant one.

The payoff of building it this way is that moving to a cloud VM is a host change
and a DSN change, not a new system: the same compose stack, with `--profile
local-db` dropped and `AQ_DATABASE_URL` pointed at managed SQL. Container
problems get found on a laptop, where debugging is cheap, rather than on a VM
behind a tunnel.

Two habits keep the two from interfering — see
[Isolation from a native install](#isolation-from-a-native-install) for the full
contract: never share a database between them, and think before bind-mounting
host paths.

## What this is, and what it deliberately is not

**It does not run `aq install`.** That installer exists to mutate a developer's
host, and its supported-platform matrix (`src/install/platform.py`) accepts only
macOS and Windows+WSL2 — a plain Linux server is refused outright. In an image
the Dockerfile *is* the install, so the matrix never applies.

**The daemon does not serve the dashboard.** It is an API server; the SPA is
served by a separate Caddy container, which also reverse-proxies `/api` to the
daemon. That gives the deployment one entry point to tunnel and one place to
terminate TLS.

**Agents run inside the daemon container** (as tmux sessions), which is the
simplest thing that works. The intended next step is a separate worker image
behind a container session provider, so each agent gets its own container and
therefore its own hard CPU/memory limits. See "Roadmap".

## Architecture

```
                     ┌──────────────────────────────┐
  you ──tunnel:8088─►│ dashboard (Caddy)            │
                     │   /            -> SPA        │
                     │   /api,/ws,... -> daemon     │
                     └───────────────┬──────────────┘
                                     │ compose network
                     ┌───────────────▼──────────────┐
                     │ daemon (API only, :8081)     │
                     │   orchestrator               │
                     │   embedded MCP               │
                     │   tmux sessions: claude/…    │
                     └───────────────┬──────────────┘
                                     │
                  PostgreSQL (local-db container, or managed)

  volumes: aq-data       -> /data/agent-queue  (vault, logs, database-free state)
           aq-workspaces -> /data/workspaces   (base clones + worktree slots)
           aq-home       -> /home/aq/.claude   (harness login + transcripts)
```

## Configuration

One image, configured entirely by environment. `deploy/config.template.yaml` is
baked in as `/etc/agent-queue/config.yaml` with `${VAR}` placeholders, and
`src/config.py` substitutes them recursively across every string at load time —
no entrypoint templating.

A referenced variable that is unset **raises at startup** rather than defaulting
silently. That is the right behaviour for a container, but it means every
`${...}` in the template must be supplied. See `.env.example`.

Validate a config without starting the daemon:

```bash
docker compose -f docker-compose.prod.yml run --rm daemon --validate-config /etc/agent-queue/config.yaml
```

The schema is `docs/reference/configuration-schema.json`. Top-level keys are
closed (`additionalProperties: false`), so a typo fails validation rather than
being ignored.

## Database

PostgreSQL only — SQLite support has been removed. Put the whole DSN in
`AQ_DATABASE_URL`; do not split the password into a separate variable.

Managed instances are a first-class path: AQ handles having no superuser by
checking whether the role and database already work instead of failing. **No
server-side extensions are required** (there is no `CREATE EXTENSION` anywhere
in `migrations/`), so a managed instance with a restricted extension allowlist
is fine.

Size `database.pool_max_size` with the agent count — every concurrent agent can
hold connections, and managed instances enforce a ceiling.

## Harness credentials

AQ never types a credential. Each provider has a documented headless path
(`src/install/logins.py`):

**The simplest path is to log in inside the container** — no API key, no secret
in the environment, and the credential persists in the `…_aq-home` volume:

```bash
docker compose -f docker-compose.prod.yml exec daemon claude auth login
```

Otherwise, per provider:

| Harness | Credential to use here |
|---|---|
| Claude | `claude auth login` (above), or **`ANTHROPIC_API_KEY`** — see the caveat below |
| Codex | `OPENAI_API_KEY`; or `codex login --device-auth` on another device |
| Gemini | `GEMINI_API_KEY`; or Vertex AI via `GOOGLE_GENAI_USE_VERTEXAI=true` + `GOOGLE_CLOUD_PROJECT` + `GOOGLE_CLOUD_LOCATION`; or `GOOGLE_APPLICATION_CREDENTIALS` |

> **`CLAUDE_CODE_OAUTH_TOKEN` does not work through the daemon environment.**
> `src/install/logins.py` offers it as the headless path, and it is listed in
> `HARNESS_CREDENTIAL_ALLOWLIST` — but `is_harness_session_marker()` in
> `src/env_scrub.py` matches every `CLAUDE_CODE_*` name unconditionally, and
> that check runs *before* the allowlist is consulted. The token is stripped
> from `os.environ` at daemon startup and dropped again in `scrub_env()`, so it
> can never reach a session. `ANTHROPIC_API_KEY` is explicitly exempted from
> that rule (`upper != "ANTHROPIC_API_KEY"`) and is what the installer's own
> comment calls "the normal install shape". Looks like an upstream bug worth
> filing; until it is fixed, use the API key.

On GCP the Vertex path is the best of these: attach a service account to the VM
and no long-lived secret lives on the box at all.

Harnesses are baked in at build time via `AQ_HARNESSES` (default `claude`).
Gemini additionally needs Node in the daemon image — add it to
`Dockerfile.daemon` before enabling that harness.

## Security posture

**Nothing is published beyond loopback, by design.** This stack runs code that
agents wrote: it compiles, tests and executes repository code. Treat the host as
a machine running semi-trusted code, not as a hardened service.

- **The network is the security boundary, not `api_auth`.** Only the dashboard
  port is published, bound to `127.0.0.1`. Reach it via SSH tunnel, IAP
  TCP-forwarding, or Tailscale.
- `api_auth.require_session_token` is **false**, and has to be: the dashboard
  sends no `Authorization` header anywhere in `dashboard/src`, so it depends on
  the middleware's "no header → `LOCAL_SCOPE`" path. Setting it true returns 401
  on every non-exempt route and leaves the dashboard an empty shell — while
  `/health` and `/ready` keep answering 200, so it still *looks* fine. A token
  would not help behind a proxy either: global-admin tokens are refused with
  "token restricted to loopback" unless the peer is `127.0.0.1`, and behind
  Caddy the peer is the proxy's container IP.
- Consequence: **anyone who reaches that port has full control.** Publishing it
  on `0.0.0.0` would hand over an unauthenticated control plane for a machine
  that runs agent-written code. Keep it on loopback.
- The daemon container publishes no ports at all.
- Containers run as a non-root `aq` user.

A genuinely public dashboard is a separate project with its own auth review.

## Sizing

Start at ~4 concurrent agents: 4 vCPU / 16 GB and ~100 GB for `/data`.

Size for the *projects*, not the agent count. An agent's own loop is mostly
waiting on the network, but agents **spawn builds and test suites** — which is
where the load actually is, and why AQ ships resource gating against "a compiler
that spawns per-core". RAM and disk dominate steady state; CPU spikes hard and
briefly. A box that looks idle on average can still be saturated exactly when it
matters.

**Scale the machine, not the instance count.** The scheduler is machine-scoped —
one daemon per database — so a second daemon against the same database
double-provisions its worker pools. See
[`DESIGN.md` §3](DESIGN.md#3-the-hard-constraint-the-scheduler-is-machine-scoped).

Keeping `/data/workspaces` on a persistent volume is what keeps a task an
incremental fetch into a reusable worktree slot rather than a fresh clone; see
[`DESIGN.md` §4](DESIGN.md#4-repository-cost-already-solved-easy-to-regress).

## Verifying the stack

A smoke test that exercises the whole chain — scheduler, workspace acquisition,
session launch, harness startup — against a throwaway repository.

```bash
D="docker compose -f docker-compose.prod.yml exec -T daemon"

# 1. A repo for the agent to work in
$D sh -c 'git config --global user.email aq@localhost;
          git config --global user.name  "Agent Queue";
          mkdir -p /data/workspaces/smoke-repo && cd /data/workspaces/smoke-repo &&
          git init -q -b main &&
          printf "def add(a, b):\n    return a + b\n" > calc.py &&
          git add -A && git commit -qm "Initial commit"'

# 2. Project + workspace
$D aq project create --name smoke --default-branch main
$D aq project add-workspace --project-id smoke --source link \
      --path /data/workspaces/smoke-repo --name smoke-main

# 3. A task. Both flags matter:
#    --intelligence-class, or the task parks on `awaiting_intelligence_route`
#      (the routing playbook that would assign one needs `playbooks.enabled`,
#      which is off by default) while sitting in READY looking dispatchable.
#    -P <profile>, or it may route to a harness this image does not install.
#    The class and profile must also match an agent that exists, or the task
#    parks on `no_idle_agent`.
$D aq task create -p smoke -t "Add subtract to calc.py" \
      -d "In calc.py add subtract(a, b) returning a - b. Then stop." \
      --intelligence-class standard-high -P standard-high-claude

# 4. Watch it
$D aq task explain --task-id <id>      # why it is or is not running ([] = nothing blocking)
$D tmux -L aq ls                       # the live agent session
$D aq task show <id>                   # 🟢 COMPLETED when done

# 5. Confirm the agent really did the work
$D sh -c 'cd /data/workspaces/smoke-repo && git show aq/<id>:calc.py'
```

`aq doctor` inside the container is the other health signal; a good run is
"43 ok · 10 info · 5 warn · 0 error". The warnings are expected on a fresh
stack: optional harness binaries absent, no project root configured, and no
`claude /usage` probe until the harness is authenticated.

### Harness authentication

Log in once per harness, inside the container:

```bash
docker compose -f docker-compose.prod.yml exec daemon claude auth login
docker compose -f docker-compose.prod.yml exec daemon codex login --device-auth
```

Both persist: `~/.claude` and `~/.codex` are each on their own volume. Codex
keeps its login in `~/.codex/auth.json` (`CODEX_HOME`), *outside* `~/.claude`,
so it needs its own mount — and gemini would need `~/.gemini` likewise.

The Claude login additionally persists because the image sets `CLAUDE_CONFIG_DIR=/home/aq/.claude`, which
puts the CLI's *entire* configuration inside the one persisted volume. Without
it, `.credentials.json` lives in `~/.claude` (persisted) while `.claude.json` —
which carries the OAuth **account linkage** — sits beside that directory and is
not, so every `--build` leaves a credential file in place and still drops you
back to "Select login method". That failure is especially confusing because
`claude auth status` run by hand reports `loggedIn: true`.

### Which harness each profile uses

The shipped profiles are not all Claude, so the image installs `claude` and
`codex` by default (`AQ_HARNESSES`):

| Profile | Harness |
|---|---|
| supervisor, planner, reviewer, final-reviewer, triage, spec-ingest, playbook-compiler, worker-claude | `claude` |
| **pr-merger**, worker-codex | **`codex`** |

AQ additionally derives a worker rung per (intelligence class x harness), so
`*-codex` profiles exist and can be routed to whether or not that CLI is
present. **An image missing a harness some profile routes to fails in the worst
way available**: the session dies instantly with an *empty* `start-stderr.log`,
because the real error goes to the tmux pane and the pane is killed with the
session:

```
nice: 'codex': No such file or directory
```

Installing a harness is not the same as authenticating it — each needs its own
credential, and an unauthenticated one fails at startup just as visibly.

If you genuinely want a harness-free image, retire the rungs rather than leaving
them routable:

```bash
for p in astra-high astra-low deep-high deep-low fast-high fast-low standard-high; do
  $D aq agent delete-profile --profile-id "$p-codex" --reason "CLI not installed"
done
```

Those rungs are **derived**, not shipped, so `aq agent profile-reseed` will not
bring them back — it only accepts the ten shipped system profiles. The
tombstones live in `vault/agent-types/.retired-defaults`; delete the entries
there and they are re-derived at the next start.

### The orchestrator's own LLM is a separate credential

`llm.provider` defaults to `anthropic` with no `api_key`. This is *not* the
harness login — it is what the orchestrator itself uses for reflection, routing
playbooks and knowledge extraction. Nothing calls it with the shipped defaults,
because `playbooks.enabled` and `memory.enabled` are both off; enable either and
you need a real API key in `llm.api_key`.

### GitHub access

AQ uses GitHub two ways, and they need separate thinking. **Plain git** handles
clone, fetch, branch, commit and push — run with `GIT_TERMINAL_PROMPT=0` and
`GIT_ASKPASS=/bin/false` so it can never prompt or silently pick up a stray
credential, with pushes served by a one-shot credential broker
(`src/git/askpass_broker.py`) that pins the whole git → remote-helper →
askpass chain by path, device, inode and owner. **The `gh` CLI** handles
everything PR-shaped: `gh pr merge`, `gh pr view --json statusCheckRollup` for
CI gating, and repository search during project onboarding.

Three ways to authenticate `gh`, with very different blast radii:

| | Who ends up holding it | Lifetime |
|---|---|---|
| `GH_TOKEN` / `GITHUB_TOKEN` | **every agent session** — it is in `HARNESS_CREDENTIAL_ALLOWLIST` | until revoked |
| `gh auth login` (device flow) | `hosts.yml` on the `…_aq-gh` volume, readable by anything running as `aq` | until revoked |
| GitHub App | **the daemon only** — `env.py` strips `AQ_INTEGRATION_GITHUB_APP_*` from every child env | ~1 hour, auto-refreshed |

**Recommended: a fine-grained PAT** for agent forge access, scoped to exactly
the repositories AQ works on, with only `contents: write` and
`pull_requests: write`. Set it in `.env` as `GH_TOKEN`.

The reasoning is the allowlist entry, which is deliberate and comments itself
as *"Forge access — agents open PRs from inside their worktree"*. Agents run
LLM-authored code, so **whatever you put there is a credential that
LLM-authored code holds**. A classic account-wide PAT gives a confused or
compromised agent your whole account; a fine-grained one gives it a bad PR on a
known repository — annoying, and revocable.

Note the asymmetry this creates: the most secure option is the one agents
cannot use. The GitHub App path is short-lived and daemon-only by construction,
which is exactly why it is unavailable to a worker. Use the App for the
daemon's own integration and merge path
(`integration.github_app`: `app_id`, `client_id`, `installation_id`,
`private_key_path`) and a fine-grained PAT for agents, rather than trying to
make one credential serve both.

**Scope the token as if it will leak.** Anything with execution on the daemon
container can read its environment — and per [Security posture](#security-posture)
the web layer currently offers no resistance to that. The network boundary is
doing all the work; the token's scope is the second line.

### Restricting what agents may do

AQ has a real capability system: `src/profiles/capabilities.py` defines three
namespaces, **denies by default**, and rejects wildcards at construction — so
`"*"` and `"mcp__github__*"` both fail rather than silently granting everything.

| Namespace | Enforced where |
|---|---|
| `harness_tools` | the CLI's own allowlist flag — best-effort, and a harness with no `tools_flag` cannot be restricted at all |
| `aq_commands` | **server-side at dispatch** — the real boundary |
| `plugin_tools` | same dispatch path |

**Enforcement is off by default.** `security.capability_enforcement` ships as
`"audit"`: violations are logged, not blocked, and `aq doctor` reports it. Set
it to `enforce` in `config.template.yaml` or none of the below has any effect.

#### "Branch and open PRs, but never merge"

A common operator policy, and it needs **three layers**, because the first two
do not hold on their own.

**1. Take `pr_merge` off the worker rungs.** The shipped `worker-claude` and
`worker-codex` templates include it deliberately: the factory policy permits a
worker to publish when "their task explicitly delegates publication". If you
would rather merging be a decision you make per task, remove it from the two
templates' `## Capabilities` block in the vault:

    /data/agent-queue/vault/agent-types/worker-claude/profile.md
    /data/agent-queue/vault/agent-types/worker-codex/profile.md

Edit the vault copy, not `src/profiles/defaults/` — this is an operator policy,
not a change to what AQ ships. Because every rung inherits from the template,
one edit covers all of them with nothing to regenerate, and the vault watcher
picks it up live without a restart.

**2. Keep `pr-merger` as the only route to a merge.** It is a shipped profile
whose entire capability set is `get_task`, `task_close`, `task_heartbeat`,
`task_comment`, `pr_merge`. Merging then happens only when you create a task
that routes to it — which is exactly "I decide".

**3. Turn on GitHub branch protection. This is the layer that actually holds.**
Agents have `Bash` and a `GH_TOKEN`, so they run `git` and `gh` *directly* in
their worktree — which is why `worker-claude` carries no `git_commit` or
`git_create_pr` in `aq_commands` at all. Removing `pr_merge` closes the AQ
command path and leaves `gh pr merge` completely untouched. A required
approving review on the default branch is what survives an agent with a shell
and a token.

Layers 1 and 2 are defence in depth around layer 3, not substitutes for it.

#### Can an agent see its own capabilities?

Partly. The harness allowlist flag tells the CLI which *tools* it has, so those
are visible to it. For `aq_commands` there is
`aq agent show-effective-profile --project-id <p> --agent-type <t>`, and agents
carry `AQ_PROFILE` in their environment, so they *can* ask — but nothing puts
the capability list into the prompt. By default an agent discovers the boundary
by hitting it, and in `audit` mode it never hits it at all.

### Diagnosing a session that dies at startup

`start-stderr.log` is written from a *pane capture*, so when the process dies
before tmux can be read it is empty — which is the common case and tells you
nothing. To see the real output, have tmux log every pane:

```bash
docker compose -f docker-compose.prod.yml exec -T daemon sh -c \
  'printf %s "set-hook -g after-new-session \"pipe-pane -o \x27cat >> /data/agent-queue/pane.log\x27\"" > ~/.tmux.conf'
# then, after the next attempt:
docker compose -f docker-compose.prod.yml exec -T daemon cat -v /data/agent-queue/pane.log
```

## Known gaps

- **Verified: an agent completes a task inside the container.** A smoke task
  created its branch, edited the file and committed — `aq/<task>` containing the
  requested change, task `COMPLETED`. What is *not* yet exercised: long runs,
  multiple concurrent agents, and anything beyond a trivial single-file edit.
- The dashboard image regenerates its typed client from the committed
  `openapi.json`. If that file drifts from the API, the dashboard builds against
  a stale spec.
- `packages/memsearch` is not installed in the daemon image. Its importers
  degrade with a warning rather than failing, so enabling `memory.enabled` would
  silently no-op until it is added. Memory is off by default.
- Embeddings default to a local Ollama, which is not in this stack — repoint at a
  hosted embedding API before enabling memory.
- No automated backup of the data volume yet; the vault lives there.

Design rationale, the scaling constraint, spot/preemptible hosts, the Kubernetes
question and the roadmap are in [`DESIGN.md`](DESIGN.md).
