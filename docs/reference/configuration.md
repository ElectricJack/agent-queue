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
| agents_config, agent_profiles, scheduling, pause_retry | Agent defaults, legacy in-config profiles, task cadence, and retry pauses. | Vault profiles are the current editable source; in-config profiles remain for compatibility. |
| llm, providers, pricing, llm_logging | Direct LLM calls, provider probes, price tables, and LLM logging. | Provider configuration is local policy; no key is shipped. |
| supervisor, supervisor_agent, sessions, worktrees, streams | Session execution, supervisor delivery, worktree behavior, and stream handling. | Some flags gate service construction and require restart. |
| memory, memory_extractor, inbox | Optional memory extension behavior, extraction, and inbox polling. | Memory data is preserved if disabled; plugin availability is separate. |
| playbooks, mcp_server, events, messages | Playbook runtime, embedded command surface, event handling, and delivery. | Each section has its own enablement/validation fields. |
| health_check, monitoring, logging, archive, auto_task | Health endpoint, task monitoring, logs, archival, and automatic task policy. | Operational settings; use the schema for bounds. |
| security, api_auth, surface, state_machine, work_graph, integration | Security/auth, agent ergonomics, task-state enforcement, graph behavior, and delivery integration. | Current settings; not evidence that historical designs are active. |
| swarm, resources, metrics, graph_layout | Pull pools, per-session limits/test slots, fleet metrics, and graph layout. | Resources defaults gate shared machine capacity. |
| global_token_budget_daily, max_daily_playbook_tokens, max_concurrent_playbook_runs, rate_limits | Installation-wide token and playbook limits. | Limits are optional except playbook concurrency's default. |

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
| agents_config, agent_profiles, archive, auto_task, global_token_budget_daily, graph_layout, llm_logging, logging, max_concurrent_playbook_runs, max_daily_playbook_tokens, metrics, monitoring, pause_retry, pricing, project_roots, providers, rate_limits, resources, scheduling, state_machine, surface, swarm, work_graph | api_auth, data_dir, database, database_path, discord, env, events, health_check, inbox, integration, llm, mcp_server, memory, memory_extractor, messages, messaging_platform, playbooks, profile, security, sessions, streams, supervisor, supervisor_agent, validate_events, workspace_dir, worktrees |

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
