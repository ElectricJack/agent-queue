# Deploying Agent Queue

A containerised deployment of the full stack — daemon, dashboard, and a managed
PostgreSQL — that runs on any Docker host: a cloud VM, or your own machine.

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

  volumes: aq-data       -> /data/agent-queue  (vault, agent homes, transcripts)
           aq-workspaces -> /data/workspaces   (base clones + worktree slots)
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

| Harness | Credential to use here |
|---|---|
| Claude | **`ANTHROPIC_API_KEY`** — see the caveat below |
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

Publishing the dashboard port on `0.0.0.0` would expose an unauthenticated
control plane. A genuinely public dashboard is a separate project with its own
auth review.

## Repository cost

A common worry is workers re-cloning repositories. AQ already avoids this, and
the volume layout here is what preserves it.

Agents run in **reusable worktree slots** at `<base_repo>/.aq/worktrees/slot-N`.
Per task, `reset_slot_for_task` does `fetch` → `reset --hard` → `clean -fd` —
never `-fdx`, so gitignored caches (`node_modules/`, `.venv/`, build output)
survive between tasks. Worktrees share the base clone's object store, so N slots
cost roughly one repository on disk, and `worktree_setup` runs only when a slot
is first created.

All of that depends on `/data/workspaces` being a **persistent volume**. A
design where each worker clones on startup would discard every one of these
properties.

## Sizing

Start at ~4 concurrent agents: 4 vCPU / 16 GB and ~100 GB for `/data`. Test
suites are the real memory spikes, not the agents themselves. Scale the machine
rather than adding hosts — the scheduler is machine-scoped (one daemon per
database; there is no host column in the schema), so a second daemon against the
same database would double-provision its worker pools.

## Roadmap

1. **Per-worker containers** — a `ContainerTmuxProvider` subclassing
   `TmuxProvider` and overriding `_tmux()` to route through `docker exec`. All
   51 tmux call sites funnel through that one method, and the worker image keeps
   tmux inside it, so pane semantics, the dashboard terminal and pane streaming
   all keep working. Each agent then gets its own container and its own
   `--cpus`/`--memory`.
2. **Terraform** — VM, disks, VPC, IAM, Secret Manager, managed SQL with a
   private IP. Deferred until the images are proven.
3. **Backups and observability** — automated SQL backups, data-disk snapshots,
   log shipping, `aq doctor` as a healthcheck.

## Known gaps

- Not yet built or run end to end; expect first-build fixes.
- The dashboard image runs the TS-client generation step, which reads the
  committed `openapi.json`. If that file drifts from the API, the dashboard is
  built against a stale spec.
- Memory (`aq-memory`) is off by default. Milvus needs no server — it defaults to
  embedded Milvus Lite — but embeddings default to a local Ollama, which would
  need adding to this stack or repointing at a hosted embedding API.
- No automated backup of the `aq-data` volume yet; the vault lives there.
