# Configuration reference

This reference describes the YAML settings AQ reads from the current implementation.
For the difference between configuration, vault Markdown, and database state, start
with [Configuration, the vault, and prompt context](../concepts/configuration-and-vault.md).

## Files and precedence

Given ~/.agent-queue/config.yaml, [src/config.py](../../src/config.py) loads settings
in this order. Later mappings deep-merge over earlier mappings; nested mappings merge
recursively and a scalar or list at the same key replaces the earlier value.

1. Read .env next to the base YAML, without replacing already-set process variables.
2. Read config.yaml.
3. Select env from AGENT_QUEUE_ENV, otherwise the base env key, otherwise production;
   if present, merge config.&lt;env&gt;.yaml.
4. Select a profile from the caller's --profile, otherwise AGENT_QUEUE_PROFILE; if
   selected, merge profiles/&lt;profile&gt;.yaml.
5. Substitute ${NAME} in all string values from the resulting environment.
6. Build typed sections and validate them before the daemon uses the configuration.

For example, config.yaml can keep portable defaults while an uncommitted development
overlay changes only local behavior:

~~~yaml
# config.yaml
env: development
database:
  url: ${AQ_DATABASE_URL}
resources:
  max_concurrent_agents: 2
~~~

~~~yaml
# config.development.yaml
logging:
  level: DEBUG
~~~

The second file does not repeat database.url. A profile overlay is applied after both
files, so it wins if all three define the same setting. AGENT_QUEUE_ENV wins over the
base file's env; a direct caller profile wins over AGENT_QUEUE_PROFILE.

> **Security.** Put a placeholder such as ${AQ_DATABASE_URL} in YAML and provide the
> value through the process environment or its adjacent .env. Do not commit a real DSN,
> token, or API key.

## Startup-required settings

database.url must be a PostgreSQL URL; SQLite is not supported. data_dir determines the
vault and compiled-artifact roots, and workspace_dir locates agent workspaces. Those
settings, the messaging platform, and subsystem-construction flags are read when the
daemon builds services. Change them, then restart the daemon.

[aq install](cli/install.md) writes ~/.agent-queue/config.yaml on a fresh machine: it
creates the file with no messaging platform selected, points the database section at
the DSN it provisioned, and adds resource-aware defaults derived from the box's cores
and memory ([default tuning](../guides/default-tuning.md)). Secrets stay in
~/.agent-queue/.env with owner-only permissions and are referenced from the YAML as
${VAR}. A section you have already written is kept, and a numbered backup is taken
before the file is changed — but neither is a replacement for reviewing the result.

## Top-level sections

The authoritative field names, nested fields, types, validation bounds, and defaults
are the AppConfig dataclasses in [src/config.py](../../src/config.py). An operator can
ask a running daemon for the same shape with:

~~~bash
aq system config schema
~~~

| Section or field | What it controls | Default/status |
|---|---|---|
| data_dir, workspace_dir, project_roots | AQ data, workspaces, and allowed onboarding roots. | Paths default under the user's home directory; project_roots is empty. |
| database, database_path | PostgreSQL pool settings and a legacy alias. | database.url is required; database_path is compatibility-only. |
| env, profile, validate_events | Overlay selection and event payload validation. | env defaults to production; profiles are optional. |
| messaging_platform, discord | No messaging or the one Discord destination, digest, and escalation controls. | Platform defaults to discord; Discord connection values are installation policy. |
| agents_config, agent_profiles, scheduling | Agent defaults, legacy in-config profiles, and task cadence. | Vault profiles are the current editable source; in-config profiles remain for compatibility. `pause_retry` was retired (it was never read) and is ignored with a warning. |
| llm, providers, pricing, llm_logging | Direct LLM calls, provider probes, price tables, and LLM logging. | Provider configuration is local policy; no key is shipped. |
| llm.fallback | An optional second direct-path credential, used only while provider availability holds the reserved `llm` key unavailable. | `null` (no fallback: direct-path calls fail fast during an outage). Restart-required like the rest of `llm`. See [`llm.fallback`](#llmfallback). |
| provider_failover | Provider availability and failover: when a harness login counts as exhausted, logged out or failing, how it recovers, operator override expiry, and the failover policy and limits the re-route sweep reads. | `mode: enforce`. Hot-reloadable. Every key is in [`provider_failover` keys](#provider_failover-keys); `aq provider status` shows the state. |
| docs | Base URL used to link contracted playbook commands to their reference pages. | Defaults to this repository's `main/docs/` tree; private mirrors can override it. |
| supervisor, supervisor_agent, sessions, worktrees, streams | Session execution, supervisor delivery, worktree behavior, and stream handling. | Some flags gate service construction and require restart. |
| memory, memory_extractor, inbox | Optional memory extension behavior, extraction, and inbox polling. | Memory data is preserved if disabled; plugin availability is separate. |
| playbooks, mcp_server, events, messages | Playbook runtime, embedded command surface, event handling, and delivery. | Each section has its own enablement/validation fields. |
| health_check, monitoring, logging, archive, auto_task | Health endpoint, task monitoring, logs, archival, and automatic task policy. | Operational settings; use the schema for bounds. |
| security, api_auth, surface, state_machine, work_graph, integration | Security/auth, agent ergonomics, task-state enforcement, graph behavior, and delivery integration. | Current settings; not evidence that historical designs are active. |
| swarm, resources, metrics, graph_layout | Pull pools, per-session limits/test slots, fleet metrics, and graph layout. | Resources defaults gate shared machine capacity. |
| dashboard_server (YAML `dashboard.server`) | The dashboard server process: `enabled`, `host` (an IP literal or `localhost`), `port` (default 8082, or 8083 when `mcp_server.port` is 8082; never the daemon's), and the origin Discord links name: `public_url`, `tailscale_path`. | Read when the dashboard server starts, so restarting it applies an edit; the daemon needs no restart. See [dashboard server settings](#dashboard-server-settings). |
| global_token_budget_daily, max_daily_playbook_tokens, max_concurrent_playbook_runs, rate_limits | Installation-wide token and playbook limits. | Limits are optional except playbook concurrency's default. |

## GitHub credentials

GitHub CLI (`gh`) must be installed on the daemon host for GitHub API and
delivery operations in **both** modes. `aq install` does not install `gh`.
Choose one credential source; AQ does not require both:

| Configuration | Effective credential |
|---|---|
| No `integration.github_app` | The daemon OS user's existing `gh` credentials: `GH_TOKEN`, then `GITHUB_TOKEN`, then its stored login. AQ leaves that login unchanged. |
| `integration.github_app` configured | An installation token scoped to the authorized repository, supplied as `GH_TOKEN` for each AQ-owned invocation. The stored login and parent environment are unchanged. App failures never fall back to a PAT or SSH key. |

For App mode, install the App on the repositories AQ will use, make its private
key readable only by the daemon OS user, and set the existing configuration
fields (there is no new setting or database migration):

~~~yaml
integration:
  github_app:
    client_id: ${AQ_GITHUB_APP_CLIENT_ID}
    app_id: 12345
    installation_id: 67890
    private_key_path: /home/aq/.agent-queue/github-app.pem
~~~

Replace the example IDs and key path with your installation's values; keep
secrets out of the YAML and version control. App mode supports AQ operations
on an authorized existing repository, including App-backed task and integration
delivery. It does not provide user-wide repository search, owner selection,
user identity, repository creation or profile gists. The current onboarding
flow can validate an explicit URL but still refuses an App-backed clone; see
[project onboarding](../guides/project-onboarding.md#github-on-the-daemon-host).

The daemon constructs its GitHub credential provider from the effective
configuration at startup. Changing the mode, App identity, installation or
private-key reference requires a **daemon restart**; `aq system reload-config`
does not switch live requests to the new credentials. Ordinary installation
token refresh happens in memory without a restart. GitHub health reports CLI
availability and credential mode separately from access to a specific
repository; App mode does not depend on `gh auth status` or a personal login.

## Provider availability settings

Two blocks configure what happens when a provider runs out of usage, loses its login
or keeps failing: `provider_failover` for the coding-agent sessions and the re-route
sweep, and `llm.fallback` for the daemon's own direct API calls. What the states and
holds mean is explained in [scheduling](../concepts/scheduling.md#provider-availability-and-failover);
what to do during an outage is the [provider outage runbook](../guides/provider-outage.md).
Defaults and bounds below are read from `ProviderFailoverConfig` and `LLMFallbackConfig`
in [src/config.py](../../src/config.py); the machine-readable form is
[configuration-schema.json](configuration-schema.json) and `aq system config schema`.

### `provider_failover` keys

Every key is optional; an absent key keeps its default, and an absent section is the
defaults. The section is **hot-reloadable**: the availability service and the re-route
engine read it through a getter on every piece of evidence and every sweep, so an edit
applies on the next tick without a restart. A value outside its bound rejects the whole
edited file, and the current configuration stays in use.

~~~yaml
provider_failover:
  mode: enforce
  classes:
    deep-high: hold          # never fail deep-high work over to another provider
  reroute:
    max_priority_value: 50   # only urgent work moves automatically
  override:
    default_ttl_seconds: 7200
~~~

**Policy.**

| Key | Default | Bounds | What it does |
|---|---|---|---|
| `mode` | `enforce` | `off`, `observe`, `enforce` | `enforce` suppresses launches against an unavailable provider and lets the sweep move work. `observe` tracks state, emits events, notifies and serves every surface, but suppresses nothing and moves nothing automatically. `off` records nothing. |
| `order` | `[]` | List of provider keys; a duplicate is a warning | Failover target preference. Empty means the project default's provider first, then `claude`, then `codex`, then any other provider. |
| `default_policy` | `same_class` | `same_class`, `hold` | What a class does when its provider is unavailable: move to the same class elsewhere, or hold. |
| `classes` | `{}` | Map of class id to `same_class` or `hold` | Per-class override of `default_policy`. |

**`usage`** — thresholds on the provider's own usage readings (the account-wide window's
used percent).

| Key | Default | Bounds | What it does |
|---|---|---|---|
| `usage.exhausted_percent` | `99.0` | `(0, 100]` | A fresh account-wide reading at or above this is `exhausted`. A model-scoped window this full only makes the provider `degraded`. |
| `usage.degraded_percent` | `85.0` | `(0, 100]`, below `exhausted_percent` | A reading at or above this is `degraded` (`usage_high`): still launched against, never a failover target. Also the "usage is high" corroboration that lets one usage dialog or rate-limit exit trip the provider. |
| `usage.hysteresis_percent` | `2.0` | `>= 0`, below `degraded_percent` | A `usage_high` provider stays degraded until its reading falls this far below `degraded_percent`. |

**`launch`** and **`rate_limit`** — how much evidence trips a provider.

| Key | Default | Bounds | What it does |
|---|---|---|---|
| `launch.strong_failures_to_trip` | `2` | `>= 1` | Consecutive startup deaths on the login (or usage-limit) dialog that trip `unauthenticated` (or `exhausted`) without other corroboration. |
| `launch.generic_failures_to_trip` | `5` | `>= 1` | Consecutive unexplained startup deaths inside the window that trip `failing` — when they span two projects, or happen where another provider launched successfully. |
| `launch.window_seconds` | `600` | `> 0` | The window those failures, rate-limit exits and the "another provider launched here" test are counted over. |
| `launch.suspect_backoff_seconds` | `30` | `>= 0` | How long a task pauses (`provider_suspect`) after the first uncorroborated provider signal, or when a recovering provider's canary launch is already in flight. |
| `rate_limit.exits_to_trip` | `2` | `>= 1` | Sessions (distinct) exiting on a provider rate limit inside the window that trip `exhausted`. |
| `rate_limit.cooldown_seconds` | `900` | `>= 0` | Backoff base for an `exhausted` provider that reported no reset time. It doubles with each failed recovery, capped at `recovery.backoff_max_seconds`. |

**`auth_probe`** and **`recovery`** — the login probe and the way back.

| Key | Default | Bounds | What it does |
|---|---|---|---|
| `auth_probe.interval_seconds` | `600` | `>= 0`; `0` disables | How often the daemon runs the harness's login-status command against a launchable provider. |
| `auth_probe.timeout_seconds` | `10` | `> 0` | How long one probe may take. |
| `recovery.reset_grace_seconds` | `60` | `>= 0` | How long after an `exhausted` provider's reset time it goes on probation. |
| `recovery.auth_probe_interval_seconds` | `120` | `> 0` | How often an `unauthenticated` provider is probed. `aq provider recheck` probes at once. |
| `recovery.failing_backoff_seconds` | `300` | `>= 0` | Backoff base for a `failing` provider; doubles with each failed canary. |
| `recovery.backoff_max_seconds` | `3600` | `>= 0` | The cap on every doubled backoff. |
| `recovery.flap_window_seconds` | `3600` | `>= 0` | After this long launchable, the backoff doubling resets. Also the window `notify.flap_threshold` counts half changes over. |

**`override`** — operator overrides set with `aq provider set-state`.

| Key | Default | Bounds | What it does |
|---|---|---|---|
| `override.default_ttl_seconds` | `14400` (4 h) | `>= 0`, at most `max_ttl_seconds` | How long an override lasts when neither `--for` nor `--until` is given. |
| `override.max_ttl_seconds` | `604800` (7 d) | `>= 0` | The longest override accepted. Only `disabled` may be set with `--no-expiry`; an `available` override always expires. |

**`reroute`** — the limits the re-route sweep (`provider_reroute`, driven by the
`provider-failover` playbook) applies to automatic moves. An operator's `--force` move
skips the trickle and the per-task limits.

| Key | Default | Bounds | What it does |
|---|---|---|---|
| `reroute.enabled` | `true` | boolean | `false` turns automatic moves off; held tasks report `failover_inactive` and wait for their provider. |
| `reroute.max_per_sweep` | `10` | `>= 1` | Most tasks one sweep moves. |
| `reroute.target_backlog_factor` | `1.0` | `> 0` | Per target rung, at most `max(1, ceil(factor × capacity))` moved-and-not-yet-started tasks are kept queued; capacity is `max_active` for a pool profile, else its enabled workers. The rest hold `awaiting_failover_capacity`. |
| `reroute.allow_degraded_target` | `false` | boolean | Let a `degraded` provider receive failover traffic. |
| `reroute.max_priority_value` | `null` | integer or `null` | When set, only tasks whose priority number is at most this move automatically; the rest hold `priority_policy_hold`. |
| `reroute.task_cooldown_seconds` | `1800` | `>= 0`; `0` disables | A task moved automatically is not moved automatically again inside this window. |
| `reroute.max_auto_per_task` | `2` | `>= 1` | After this many automatic moves a task holds for a human (`reroute_limit_reached`). |

**`notify`**, **`doctor`** and **`evidence`**.

| Key | Default | Bounds | What it does |
|---|---|---|---|
| `notify.supervisor` | `true` | boolean | Send the half-change message to the global supervisor and `user:dashboard`, and the per-project notice for each re-route batch. Both also need `messages.enabled`. |
| `notify.digest` | `true` | boolean | Include provider state changes in the hourly digest, as fleet facts. |
| `notify.escalate_unauthenticated` | `true` | boolean | File an escalation when a provider is logged out. |
| `notify.escalate_failing_after_seconds` | `1800` | `>= 0` | File an escalation when a provider has been `failing` this long. |
| `notify.escalate_all_down_after_seconds` | `1800` | `>= 0` | While every session provider is unavailable, file a `critical` escalation unless one is due back within this long. |
| `notify.flap_threshold` | `3` | `>= 1` | More half changes than this inside `recovery.flap_window_seconds` is flapping: one message says so, and per-change messages pause for a window. |
| `doctor.held_warn_seconds` | `14400` (4 h) | `>= 0` | `aq doctor --check providers.held_tasks` warns about work held longer than this. |
| `evidence.keep` | `20` | `>= 1`, and at least the largest trip count plus 2 | Evidence items kept per provider, newest first. The trip rules count over this ring. |

`pause_retry` was an earlier section for provider backoff; nothing read it. It is retired
and ignored with a warning.

### `llm.fallback`

An optional second credential for the direct path (playbook `llm` steps, including
default-assignment-routing, plugin `invoke_llm`, stub enrichment). Provider availability
tracks direct-path calls under the reserved key `llm`. While that key is unavailable and
`provider_failover.mode` is `enforce`, calls resolve their intelligence class against the
fallback provider's slice and are made with this credential; a class with no slice there,
or no fallback at all, fails fast with `provider_error` (diagnostic
`provider_unavailable`) instead of waiting. A session harness's login is never borrowed
([src/llm/client.py](../../src/llm/client.py)).

~~~yaml
llm:
  provider: anthropic
  fallback:
    provider: openai
    api_key: ${OPENAI_API_KEY}
~~~

| Key | Default | What it does |
|---|---|---|
| `fallback` | `null` | Absent or `null`: no fallback. Otherwise a mapping of the keys below. |
| `fallback.provider` | — (required) | `anthropic`, `google` or `openai`. |
| `fallback.api_key` | `""` | The credential; empty means the provider's own `*_API_KEY` environment variable. |
| `fallback.base_url` | `""` | `openai` only: an OpenAI-compatible endpoint. |
| `fallback.model` | `""` | An explicit model id; empty means the intelligence class, else the provider default. |
| `fallback.default_class` | `""` | The class used when a call names none. |

The fallback must differ from the primary in `provider`, `api_key` or `base_url` — the
same credential is unavailable exactly when the primary is, so an identical block is a
validation error. Only `max_tokens` is shared with the primary. Unknown keys are ignored
with a warning that names them (never their values). `llm` is restart-required, so a
change to `llm.fallback` takes effect when the daemon restarts.

## Dashboard server settings

The daemon serves an API only; the browser dashboard comes from the separate
dashboard server process (see [architecture](../concepts/architecture.md#two-processes-the-daemon-and-the-dashboard-server)).
Its section is `dashboard.server` — a top-level `dashboard_server:` block is read
the same way, which is what the config editor writes. Defaults and bounds are
`DashboardServerConfig` in [src/config.py](../../src/config.py):

~~~yaml
dashboard:
  server:
    enabled: true      # `aq start` manages it when a verified bundle is installed
    host: 127.0.0.1    # an IP literal or `localhost`
    port: 8082
    public_url: ""     # the origin Discord links name, e.g. https://aq.your-tailnet.ts.net
    tailscale_path: "" # empty: `tailscale` from PATH
~~~

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Whether `aq start` and `aq restart` start the dashboard server. With `false` they start only the daemon (`aq stop` still stops a dashboard server that is running), and the daemon's `/dashboard` pointer answers `404` with `"dashboard_url": null` instead of redirecting to the dashboard server. There is deliberately no setting that makes the daemon serve the dashboard again. |
| `host` | `127.0.0.1` | The bind address. Must be an IP literal or `localhost` — never a DNS name, which could be re-pointed. `0.0.0.0`, `::` or a LAN address exposes the operator console to that network; read [what a LAN bind exposes](../guides/dashboard.md#reaching-it-from-another-machine) first. |
| `port` | `8082` | The bind port, 1–65535, and never `mcp_server.port` (a validation error). When unset and `mcp_server.port` is 8082, the default steps aside to **8083**, so a daemon moved to 8082 before the dashboard server existed keeps a config that loads. A port you set is never moved, and a busy port is a startup failure, never an auto-increment: the URL stays predictable for bookmarks and the installer. |
| `public_url` | `""` | The origin that externally posted links name: escalations, the hourly digest and document reviews, plus `digest_preview`. It must be an `http(s)` origin with no path, query, fragment or credentials, and never loopback. Normally this is your authenticated tailnet proxy, listed in `api_auth.trusted_dashboard_origins` as well. Also accepted as `dashboard.public_url`; two different values are a validation error, while an invalid value is a warning and turns links into an "unavailable" notice. Empty means posts carry that notice, unless `host` is this machine's Tailscale address. `health_check.base_url` is never used instead. The daemon picks up an edit without a restart. See [dashboard links in Discord posts](../guides/dashboard.md#dashboard-links-in-discord-posts). |
| `tailscale_path` | `""` | The Tailscale CLI used to confirm that a tailnet `host` is this machine's (`tailscale ip`, 2-second deadline). Empty means `tailscale` from `PATH`. It is consulted only when `public_url` is empty and `host` is a Tailscale address. |

The dashboard server reads these once, when it starts, so `aq dashboard restart`
applies an edit and the daemon needs no restart. It reads the YAML directly
(never the daemon's `.env`) through the daemon's own parsers, so a value means
the same thing in both processes ([src/dashboard_server/settings.py](../../src/dashboard_server/settings.py)).
The daemon's API base it forwards to is resolved like every `aq` command's:
`AQ_API_URL`, else `mcp_server.host` and `mcp_server.port`.

**`api_auth.trusted_dashboard_origins`** keeps one meaning in both processes:
the origins, besides literal loopback, from which a browser may drive this
install. Through the default loopback bind the list can stay empty — terminals
included. A LAN address, DNS name or TLS front end the browser uses must be
listed as its exact origin (`https://aq.example.test`), or the dashboard server
answers `421 misdirected_host` (unknown `Host`) or `403 origin_not_allowed`
(unknown `Origin`), and the daemon refuses its terminals `4403`.

`aq doctor` checks the section without importing the dashboard server:
`dashboard.server.running`, `dashboard.server.bundle`, `dashboard.server.port`,
`dashboard.server.exposure` (a warning while `host` is not loopback) and
`dashboard.remote_link` (the origin Discord links name, whether the edge accepts
it, and a warning when Discord posts carry the "unavailable" notice)
([src/doctor/dashboard_server_checks.py](../../src/doctor/dashboard_server_checks.py)).
`aq dashboard link` prints the same report from the YAML, without the daemon.

## Reload and restart

ConfigWatcher polls the configuration file, validates a changed version, compares
top-level sections, applies the hot sections to its current config object, and emits
config.reloaded. It emits config.restart_needed for every other changed section and
does not partially apply them. An invalid edited file is rejected and the current
configuration stays in use.

Run this after an operator edits YAML to see the actual classification and result:

~~~bash
aq system reload-config
~~~

| Changes applied without daemon restart | Changes that require restart |
|---|---|
| agents_config, agent_profiles, archive, auto_task, dashboard_server, docs, global_token_budget_daily, graph_layout, llm_logging, logging, max_concurrent_playbook_runs, max_daily_playbook_tokens, metrics, monitoring, pricing, project_roots, provider_failover, providers, rate_limits, resources, scheduling, state_machine, surface, swarm, work_graph | api_auth, data_dir, database, database_path, discord, env, events, health_check, inbox, integration, llm, mcp_server, memory, memory_extractor, messages, messaging_platform, playbooks, profile, security, sessions, streams, supervisor, supervisor_agent, validate_events, workspace_dir, worktrees |

The `integration` restart classification includes `integration.github_app`.
Changing a process environment token or App key reference also requires a
daemon restart to create a new effective provider; an expiring App
installation token refreshes automatically within that provider.

The dashboard/CLI editor reads raw YAML so ${NAME} references survive an edit. Its
round-trip writer preserves comments, order, and quote style outside the changed section;
the writer itself deliberately leaves validation to its caller. See
[src/config_editor.py](../../src/config_editor.py).

## Recovery reference

| Problem | Read-only diagnosis | Recovery |
|---|---|---|
| Unknown field/default/bound | aq system config schema | Correct YAML to the returned schema. |
| A ${NAME} is unresolved | aq system config get shows raw YAML without exposing the resolved value | Set it in the process environment or adjacent .env, then reload/restart as classified. |
| Edited value did not take effect | aq system reload-config | Restart if the response lists the changed section under restart_required. |
| YAML edit lost a comment or placeholder | Inspect raw config with aq system config get | Use the round-trip config editor/command rather than a serializer that resolves environment references. |
| Database validation fails | Read the error; it names the invalid field | Supply a PostgreSQL DSN. Do not run a worker-side Alembic command to compensate. |

## Source and focused tests

* [src/config.py](../../src/config.py) — typed schema, overlays, substitution,
  validation, diffing, and watcher.
* [src/config_editor.py](../../src/config_editor.py) — raw read/schema generation and
  round-trip writing.
* [src/install/onboarding.py](../../src/install/onboarding.py) — the installer steps
  that create, tune and validate the configuration on a fresh machine.
* aq test tests/test_config.py tests/test_config_profiles.py tests/test_config_watcher.py
  and aq test tests/test_config_editor.py tests/test_config_roundtrip.py exercise the
  focused behavior.
