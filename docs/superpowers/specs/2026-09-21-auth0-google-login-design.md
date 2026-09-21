# Auth0 + Google login for the web layer

**Date:** 2026-09-21
**Status:** proposed — nothing here is implemented
**Scope:** `src/api/` (middleware, auth, scope, websocket, terminal_stream), `dashboard/src/api/`, `deploy/docker/Caddyfile`, `src/config.py`

## 1. The problem

The daemon's HTTP API has **no concept of a human user**, and in the
configuration the deployment actually runs, no authentication either.

`TokenAuthMiddleware` (`src/api/middleware.py:55`) resolves every request to a
`RequestScope`. A request with no `Authorization` header gets `LOCAL_SCOPE`
(`src/api/auth.py:71`), and `check_command_scope` short-circuits on it:

> `src/api/scope.py:129` — `if scope.kind == "local": return None`

That bypasses the whole policy — the onboarding gate, the integration-control
gate, the `AGENT_COMMAND_SET` allowlist. Downstream, a non-session scope becomes
`TRUSTED_LOCAL`, whose `enforced` property is `False`
(`src/commands/principal.py:99`), so the capability engine is a no-op too.

**This is not theoretical.** Against the deployed stack, through Caddy, with no
credential of any kind:

```
POST /api/streams  {"command":["id"], "cwd":"<a workspace>", "session_id":"..."}
→ 200 {"status":"running"}
→ tail: uid=1000(aq) gid=1000(aq)
```

`_can_start` is `scope.kind == "local" or scope.elevated`
(`src/api/streams.py:255`), argv is validated only as a non-empty list of
strings (`src/api/streams.py:394`), and it reaches
`asyncio.create_subprocess_exec` (`src/api/streams.py:271`). `cwd` is fenced to
a workspace; the command is not. `POST /api/system/session-token` is similarly
reachable and mints durable DB-backed bearer tokens, its only guard being a
project check that is inert when `project_id is None`.

**The existing switch is not a migration path.** Setting
`api_auth.require_session_token: true` rejects every request without an `aqs_`
bearer — and `dashboard/src` sends no `Authorization` header anywhere, including
on both WebSockets, which is what drives the entire UI. Flipping it does not
secure the dashboard; it bricks it.

Today the only real control is the network: the deployment publishes on
loopback and is reached through an IAP tunnel (`deploy/README.md`). That is
sufficient for one operator and is a hard ceiling on everything else — no second
user, no audit trail, no browser access without a tunnel.

## 2. What the codebase gives us, and what it takes away

### 2.1 Helpful

- **One assignment point.** `request.state.scope` is written at exactly one
  line (`src/api/middleware.py:100`) and read through a consistent
  `getattr(request.state, "scope", LOCAL_SCOPE)` idiom at ~20 call sites.
- **`RequestScope` is a frozen dataclass** (`src/api/auth.py:28`) with room for
  identity fields; `ExecutionPrincipal` (`src/commands/principal.py:81`) already
  has the "who is running this and what may they run" vocabulary, a
  narrow-only discipline, and an audit/enforce rollout flag.
- **Same origin.** Caddy serves the SPA and proxies `/api`, `/health`, `/ready`,
  `/ws` from one origin (`deploy/docker/Caddyfile`). **A session cookie set on
  that origin is sent on every request and both WebSockets automatically, with
  zero dashboard changes.** There is no CORS anywhere in the codebase, and a
  same-origin design needs none.
- **One client injection point** if we ever want a header instead:
  `dashboard/src/api/client.ts` plus `legacy-fetch.ts`.

### 2.2 Hostile

- **WebSockets bypass HTTP middleware entirely.** `TokenAuthMiddleware`
  subclasses `BaseHTTPMiddleware`, which only sees `scope["type"] == "http"`.
  `/ws/events` (`src/api/websocket.py:271`) and `/ws/terminal/{id}`
  (`src/api/terminal_stream.py:353`) each re-implement auth inline, and
  *differently* — `/ws/events` does not apply the loopback fence that the HTTP
  path does. **Auth added the obvious way protects every REST route and leaves
  the fleet-wide event stream wide open.**
- **The loopback fence is already inoperative behind the proxy.** The
  global-admin check reads `request.client.host` (`src/api/middleware.py:81`),
  which is Caddy's container IP. Uvicorn is started without `proxy_headers`
  (`src/embedded_mcp.py:158`), so `X-Forwarded-For` is not trusted — correctly.
  **Turning `proxy_headers` on to "fix" this inverts it**: Caddy *appends* to
  `X-Forwarded-For` and uvicorn takes the leftmost entry, so a client sending
  `X-Forwarded-For: 127.0.0.1` becomes loopback. That would re-enable
  global-admin tokens and the raw-PTY terminal from anywhere.
- **Agents must keep working unchanged.** They dial loopback *inside* the
  daemon container (`src/sessions/spec.py:902`) and already send
  `Authorization: Bearer <AQ_API_TOKEN>` (`src/cli/client.py:187`). Any new
  check must short-circuit on a valid `aqs_` bearer. Conversely: **a check that
  trusts loopback is trusting every agent process**, and agents run LLM-authored
  code.
- **`_EXEMPT_PATHS` is narrower than it looks** (`src/api/middleware.py:37`). It
  exempts only from the *no-header* 401; a bad credential on `/health` still
  401s, which would break the container healthcheck.
- **The MCP sub-app is mounted at `/`** (`src/embedded_mcp.py:143`), after all
  FastAPI routes. Any unmatched path falls through to it. Caddy's `@daemon`
  matcher is an explicit allowlist and does not include `/mcp` — broadening it
  carelessly would expose ~150 CommandHandler commands.
- **No user identity exists anywhere.** No users table among the 51 in
  `src/database/tables.py`; `api_session_tokens` binds a token to a *machine*
  session; the `events` table has no actor column; `command.invoked` records
  `session_id`/`task_id`/`project_id`, all `None` for a dashboard caller. The
  audit trail literally says "something ran `delete_project`".
  `PrincipalKind` is `{LOCAL, SERVICE, SESSION, PLAYBOOK}` — there is no `USER`.

## 3. Options

### Option A — Auth0 at the edge (recommended for v1)

Put an OIDC handler in front of the daemon. Caddy `forward_auth` to
`oauth2-proxy` configured against an Auth0 tenant with a Google social
connection; unauthenticated requests are redirected to Auth0, and the proxy sets
a signed session cookie on the same origin.

**Why it fits this codebase specifically:**

- **Zero AQ code changes.** No new middleware, no `RequestScope` field, no
  config schema change.
- **Both WebSockets are covered for free** — they are proxied through the same
  Caddy and carry the same-origin cookie, so §2.2's worst landmine does not
  apply.
- **Agents are unaffected** — they never traverse Caddy.
- **It closes the actual hole today.** The unauthenticated RCE and the token
  minter stop being reachable by anything that reaches the port.

**What it does not give you:** AQ still sees `LOCAL_SCOPE` for every request.
There is no per-user authorization, and the audit trail still has no actor. It
is authentication, not accountability.

**The one thing that must not be got wrong:** the daemon must refuse requests
that do not come from the proxy. There is no trusted-proxy allowlist anywhere in
`src/api/`, so an injected identity header would be forgeable by anything that
can reach `daemon:8081` — which on the compose network includes every agent
container. Bind the daemon to the proxy's network only, and do not enable
`proxy_headers`.

### Option B — native OIDC in the daemon

Add `kind="user"` to `RequestScope`, verify Auth0 JWTs (JWKS fetch, caching,
rotation, `iss`/`aud`/`exp`, alg confusion guards), carry a signed session
cookie, add a `USER` `PrincipalKind`, a users/roles schema with a migration, and
per-user policy in `check_command_scope`.

**Why not first:** `check_command_scope` treats `"local"` as *unconditionally
allow everything* (`src/api/scope.py:129`). A new `kind="user"` gets **no policy
at all** until one is written — and mapping Auth0 users onto `"local"` to avoid
that work makes the anonymous-local hole permanent *and* remote. It also needs
CSRF for the first time (a cookie carrying authority, every route a `POST`, no
CORS to lean on), both WebSocket paths edited separately, and dashboard login
UX that does not exist (`client.ts:32` turns a 401 into an opaque `Error`).

This is the right destination. It is not the right first step.

### Option C — A then B

Ship A to stop the bleeding. Then, if and only if multi-user authorization or
an actor-attributed audit trail is actually wanted, do B behind the proxy, where
a mistake fails closed rather than open.

## 4. Recommendation

**Option C.** Deploy A now; treat B as a separate, later piece of work.

The deciding argument is that A is the only option that is *strictly additive*.
It changes no AQ behaviour, cannot break agents, and covers the WebSocket paths
that any in-daemon approach would silently miss. B touches the one function
whose default is "allow everything", which is the worst place in the codebase to
be learning as you go.

## 5. Phase A — the work

1. **`oauth2-proxy` container** in `deploy/docker-compose.prod.yml`, on the
   compose network, not published.
2. **Auth0 tenant**: a Regular Web Application, Google social connection
   enabled, callback `https://<host>/oauth2/callback`, and an **allowlist of
   permitted emails or a domain restriction** — a Google connection with no
   restriction means anyone with a Google account.
3. **Caddyfile**: `forward_auth` for everything except `/health` and `/ready`
   (the container healthcheck must stay anonymous), plus `/oauth2/*` routed to
   the proxy. Keep `@daemon` an explicit allowlist; do not add a catch-all,
   because of the MCP mount.
4. **TLS**, because a session cookie over plaintext through a tunnel is still a
   cookie in the clear on the VM's loopback. Either terminate at Caddy with a
   real certificate or keep the tunnel and set `Secure`/`SameSite` deliberately.
5. **Do not enable `proxy_headers`** on uvicorn. The loopback fence is currently
   fail-closed behind the proxy; leave it that way.
6. **Verify the hole is closed** by re-running the `POST /api/streams` probe in
   §1 and confirming a redirect rather than a 200.

## 6. Open questions

- **Is browser-reachable access wanted at all?** If the tunnel is acceptable
  forever, none of this is needed. Phase A is worth doing the moment the answer
  is "someone other than me should open the dashboard".
- **Does the terminal WebSocket need to survive?** It additionally requires the
  peer to be loopback (`src/api/terminal_stream.py:143`), which is already false
  behind Caddy. Phase A does not fix that; it would need its own decision.
- **Accountability, or just authentication?** If "who deleted that project" must
  be answerable, Phase A does not deliver it and B is required.
- **Which is the real boundary afterwards?** Phase A still leaves an
  unauthenticated RCE reachable from *inside* the compose network. The network
  posture in `deploy/README.md` stays load-bearing either way.

## 7. Related

- `deploy/README.md` — security posture, and why `require_session_token` cannot
  simply be turned on.
- `deploy/DESIGN.md` §8 — the deployment target this would sit in front of.
- `docs/reference/api/conventions.md` — `trusted_dashboard_origins`, whose only
  consumer is the terminal WebSocket.
