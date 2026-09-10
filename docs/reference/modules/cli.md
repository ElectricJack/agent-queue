# Module catalog: CLI, command layer, contracts and agent tools

Every production module behind the `aq` command line, the command handler it
dispatches to, the typed command contracts, and the agent-facing tool layer.
This is the `cli` shard of the [module catalog](README.md); the pages in the
**Component** column are where each module's behaviour is explained in prose.

Paths are relative links into the source. Private helpers share a component
page with the module they support, but every module has its own row.

## The `aq` command line — `src/cli/`

The Click application: one module per hand-written command group, plus the
generation, transport, output and formatting layers they share.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/cli/\_\_init\_\_.py](../../../src/cli/__init__.py) | Marks the CLI package; holds no commands. | [cli/README.md](../cli/README.md) | — |
| [src/cli/app.py](../../../src/cli/app.py) | Builds the root Click group, registers every command module, maps exceptions to exit codes, and mounts plugin CLI groups. | [cli/README.md](../cli/README.md) | `tests/test_cli.py`, `tests/test_cli_module_entry.py`. Import order matters: hand-written modules register before generation. |
| [src/cli/adapters.py](../../../src/cli/adapters.py) | Normalises daemon responses into the proxy objects the Rich formatters expect. | [cli/README.md](../cli/README.md) | `tests/test_cli_formatters.py` |
| [src/cli/agent_messages.py](../../../src/cli/agent_messages.py) | Implements `aq agent message` — guidance to a live worker, with delivery status. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_messages.py` |
| [src/cli/agent_surface.py](../../../src/cli/agent_surface.py) | Implements the agent-facing commands: `aq schema`, `aq prime`, `aq handoff`, `aq subagent event`, and the claim-fenced `aq task claim\|close\|heartbeat`. | [cli/prime.md](../cli/prime.md) | `tests/test_cli_agent_surface.py`, `tests/test_swarm_surface.py` |
| [src/cli/auto_commands.py](../../../src/cli/auto_commands.py) | Generates one Click command per tool definition, grouped by category, and owns the JSON/nullable argument types. | [cli/README.md](../cli/README.md) | `tests/test_cli_conformance.py`, `tests/test_cli_structured_params.py` |
| [src/cli/claim_epoch.py](../../../src/cli/claim_epoch.py) | Resolves a pool session's claim epoch from `.aq/claim.json` or `$AQ_CLAIM_EPOCH` and supplies the shared `--claim-epoch` option. | [cli/README.md](../cli/README.md) | `tests/test_swarm_surface.py`. A separate module purely to break an import cycle. |
| [src/cli/client.py](../../../src/cli/client.py) | Calls the daemon over HTTP, resolves the base URL and bearer token, and turns HTTP failures into the CLI's typed exceptions. | [cli/README.md](../cli/README.md) | `tests/test_cli_client_generated.py`, `tests/test_cli_response_failures.py` |
| [src/cli/daemon.py](../../../src/cli/daemon.py) | Implements `aq start`, `aq stop` and `aq restart`, including session teardown. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_daemon.py`, `tests/test_cli_stop_sessions.py` |
| [src/cli/db.py](../../../src/cli/db.py) | Implements `aq db current\|upgrade\|import-sqlite` — the operator's only migration door. | [cli/commands.md](../cli/commands.md) | Guarded by `src/database/migration_guard.py`; refuses `--json`. |
| [src/cli/doctor.py](../../../src/cli/doctor.py) | Implements `aq doctor` and `aq costs`. | [cli/commands.md](../cli/commands.md) | `tests/test_provider_doctor.py`, `tests/test_session_doctor.py` |
| [src/cli/envelope.py](../../../src/cli/envelope.py) | Builds the versioned JSON envelope, the brief projections and the single `emit()` output funnel. | [cli/README.md](../cli/README.md) | `tests/test_cli_envelope.py`, `tests/test_emit_schema_compliance.py` |
| [src/cli/exceptions.py](../../../src/cli/exceptions.py) | Defines the CLI exception types that carry the error code and exit status. | [cli/README.md](../cli/README.md) | `tests/test_cli_response_failures.py` |
| [src/cli/formatter_registry.py](../../../src/cli/formatter_registry.py) | Maps a command to its Rich formatter and declares which field of a result is the logical collection. | [cli/README.md](../cli/README.md) | `tests/test_cli_formatters.py`. The same declaration shapes JSON output. |
| [src/cli/formatters.py](../../../src/cli/formatters.py) | Renders tables and panels for every entity the CLI prints. | [cli/README.md](../cli/README.md) | `tests/test_cli_formatters.py` |
| [src/cli/formulas.py](../../../src/cli/formulas.py) | Implements `aq formula show` and `aq formula cook`, including repeatable `--var k=v`. | [cli/commands.md](../cli/commands.md) | `tests/test_formula_surface.py`, `tests/test_formula_commands.py` |
| [src/cli/global_options.py](../../../src/cli/global_options.py) | Copies `--json`, `--brief` and `--api-url` onto every command so they parse at any position. | [cli/README.md](../cli/README.md) | `tests/test_cli_global_options.py`. Must run last in `app.py`. |
| [src/cli/integration.py](../../../src/cli/integration.py) | Implements the `aq integration` control verbs with positional identities and compare-and-set fences. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_integration.py` |
| [src/cli/install.py](../../../src/cli/install.py) | Implements `aq install` — builds the step registry, runs the installer engine in-process with no daemon, and maps the outcome onto the documented exit codes. | [cli/install.md](../cli/install.md) | `tests/test_install_cli.py` |
| [src/cli/inventory.py](../../../src/cli/inventory.py) | Walks the live Click tree to produce the command inventory, its alias and deprecation ledgers, and its validation. | [cli/README.md](../cli/README.md) | `tests/test_cli_inventory.py`; artifact at `docs/reference/cli-command-inventory.json`. |
| [src/cli/logs.py](../../../src/cli/logs.py) | Implements `aq logs` — tails and filters the JSONL log file directly, with no daemon. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_logs.py` |
| [src/cli/menus.py](../../../src/cli/menus.py) | Provides the interactive prompts: the task wizard, fuzzy select, confirmations. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_menus.py` |
| [src/cli/messages.py](../../../src/cli/messages.py) | Implements `aq message *`, the hook-safe `aq inbox`, `aq reply` and `aq chat`. | [cli/prime.md](../cli/prime.md) | `tests/test_cli_messages.py` |
| [src/cli/playbook.py](../../../src/cli/playbook.py) | Declares the `aq playbook` group the generated playbook commands mount into. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_module_map.py` |
| [src/cli/plugins.py](../../../src/cli/plugins.py) | Implements `aq plugin *`, including the direct-database and filesystem paths install and removal need. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_plugins.py` |
| [src/cli/projects.py](../../../src/cli/projects.py) | Implements the composite `aq project` commands, including the `onboard` wizard. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_projects.py`, `tests/test_project_onboarding_commands.py` |
| [src/cli/questions.py](../../../src/cli/questions.py) | Implements `aq question list\|answer\|escalate` against the scoped daemon API. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_questions.py`, `tests/test_agent_questions.py` |
| [src/cli/sessions.py](../../../src/cli/sessions.py) | Implements `aq session *`, taking `SESSION_ID` positionally and defaulting it from `$AQ_SESSION_ID`. | [cli/commands.md](../cli/commands.md) | `tests/test_session_commands.py`, `tests/test_cli_session_history.py` |
| [src/cli/streams.py](../../../src/cli/streams.py) | Implements `aq stream start\|tail\|kill` for the dashboard's console pane. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_streams.py` |
| [src/cli/styles.py](../../../src/cli/styles.py) | Defines the Rich theme, status colours and icons every formatter uses. | [cli/README.md](../cli/README.md) | Selected by `AQ_THEME`. |
| [src/cli/system_config.py](../../../src/cli/system_config.py) | Implements `aq system config get\|set\|schema\|edit` against the YAML config file. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_system_config.py`. `edit` validates before it swaps the file. |
| [src/cli/tasks.py](../../../src/cli/tasks.py) | Implements the hand-written `aq task` commands: the create wizard, `show`, `set`, `comment`, `list`, `search`, `select`, `stop`, `restart`. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_task_create_output.py`, `tests/test_cli_task_comments.py`, `tests/test_cli_task_create_requires_kinds.py` |
| [src/cli/test_runner.py](../../../src/cli/test_runner.py) | Runs pytest behind the box-wide slot semaphore, folding in the worker cap and default marker deselects. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_test_runner.py`; see [resource gating](../../guides/resource-gating.md). |
| [src/cli/vault.py](../../../src/cli/vault.py) | Implements `aq vault migrate` and `aq vault reset-harness`. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_vault.py`, `tests/test_cli_vault_reset_harness.py` |

## The command layer — `src/commands/`

`CommandHandler` is the single entry point for every state change, whatever
surface it arrived on. It is composed from one mixin per domain; each mixin
supplies `_cmd_<name>` methods that `execute()` dispatches to.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/commands/\_\_init\_\_.py](../../../src/commands/__init__.py) | Marks the package that composes `CommandHandler` from its mixins. | [cli/commands.md](../cli/commands.md) | — |
| [src/commands/handler.py](../../../src/commands/handler.py) | Dispatches every command: strips server-owned arguments, derives the principal, applies the pause and capability gates, then calls the mixin method or a plugin handler. | [cli/README.md](../cli/README.md) | `tests/test_command_dispatch.py`, `tests/test_command_invoked_event.py` |
| [src/commands/principal.py](../../../src/commands/principal.py) | Defines the immutable execution identity and the request-local principal context. | [cli/README.md](../cli/README.md) | `tests/test_command_capability_authorization.py`. Narrowing only; never parsed from a request body. |
| [src/commands/authorization.py](../../../src/commands/authorization.py) | Decides whether a principal may dispatch a command, and filters tool schemas with the same predicate. | [cli/README.md](../cli/README.md) | `tests/test_command_capability_authorization.py`, `tests/test_capability_policy.py` |
| [src/commands/helpers.py](../../../src/commands/helpers.py) | Provides the shared helpers the mixins use to resolve projects, tasks and agents and to shape results. | [cli/commands.md](../cli/commands.md) | Exercised through the mixins' own tests. |
| [src/commands/agent_commands.py](../../../src/commands/agent_commands.py) | Implements the agent listing and workspace commands. | [cli/commands.md](../cli/commands.md) | `tests/test_agent_flock.py` |
| [src/commands/ci_commands.py](../../../src/commands/ci_commands.py) | Implements `ci_baseline_status` — the read-only CI verdict for a default-branch head. | [cli/commands.md](../cli/commands.md) | `tests/test_ci_baseline_status.py`, `tests/test_ci_main_sentinel.py` |
| [src/commands/claim_commands.py](../../../src/commands/claim_commands.py) | Implements `task_claim` — pull-based work selection with the epoch fence. | [cli/commands.md](../cli/commands.md) | `tests/test_claim_commands.py`, `tests/test_swarm_surface.py` |
| [src/commands/digest_commands.py](../../../src/commands/digest_commands.py) | Implements the read-only digest preview and schedule status. | [cli/commands.md](../cli/commands.md) | `tests/test_digest_commands.py` |
| [src/commands/discord_commands.py](../../../src/commands/discord_commands.py) | Implements shared-channel Discord housekeeping. | [cli/commands.md](../cli/commands.md) | `tests/test_discord_housekeeping.py` |
| [src/commands/escalation_commands.py](../../../src/commands/escalation_commands.py) | Implements the scoped surface for durable human escalations. | [cli/commands.md](../cli/commands.md) | `tests/test_escalation_commands.py`, `tests/test_escalation_intake.py` |
| [src/commands/event_commands.py](../../../src/commands/event_commands.py) | Implements the recent-event, token-usage and log-reading commands. | [cli/commands.md](../cli/commands.md) | `tests/test_event_commands.py` |
| [src/commands/flock_commands.py](../../../src/commands/flock_commands.py) | Implements the global agent registry commands. | [cli/commands.md](../cli/commands.md) | `tests/test_agent_flock.py`, `tests/test_agent_flock_lifecycle.py` |
| [src/commands/formula_commands.py](../../../src/commands/formula_commands.py) | Implements `formula_list`, `formula_show` and `formula_cook`. | [cli/commands.md](../cli/commands.md) | `tests/test_formula_commands.py` |
| [src/commands/gate_commands.py](../../../src/commands/gate_commands.py) | Implements creating, listing, showing and resolving work-graph gates. | [cli/commands.md](../cli/commands.md) | `tests/test_gate_queries.py` |
| [src/commands/git_commands.py](../../../src/commands/git_commands.py) | Implements the core-owned git commands, including `pr_merge`. | [cli/commands.md](../cli/commands.md) | `tests/test_git_command_handlers.py`, `tests/test_worker_git_scope.py` |
| [src/commands/graph_commands.py](../../../src/commands/graph_commands.py) | Implements the spatial layout rebuild and tidy jobs. | [cli/commands.md](../cli/commands.md) | `tests/test_api_graph_layout.py` |
| [src/commands/integration_commands.py](../../../src/commands/integration_commands.py) | Implements the hierarchical-integration primitives the delivery train is built from. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_integration.py`, `tests/test_integration_contracts.py` |
| [src/commands/mcp_commands.py](../../../src/commands/mcp_commands.py) | Implements MCP registry CRUD plus the probe and catalog reads. | [cli/agent-tools.md](../cli/agent-tools.md) | `tests/test_mcp_commands.py`, `tests/test_mcp_registry.py` |
| [src/commands/message_commands.py](../../../src/commands/message_commands.py) | Implements the inter-agent and user message queue. | [cli/prime.md](../cli/prime.md) | `tests/test_api_messages.py`, `tests/test_session_lens.py` |
| [src/commands/notes_commands.py](../../../src/commands/notes_commands.py) | Resolves note paths for the notes plugin's commands. | [cli/commands.md](../cli/commands.md) | `tests/test_notes_plugin.py` |
| [src/commands/ops_commands.py](../../../src/commands/ops_commands.py) | Implements `doctor` and the cost rollups. | [cli/commands.md](../cli/commands.md) | `tests/test_provider_doctor.py`, `tests/test_pool_doctor.py` |
| [src/commands/playbook_commands.py](../../../src/commands/playbook_commands.py) | Implements the operational playbook commands: list, run, resume, cancel, health. | [cli/commands.md](../cli/commands.md) | `tests/test_playbook_v2_commands.py` |
| [src/commands/playbook_v2_commands.py](../../../src/commands/playbook_v2_commands.py) | Implements the Playbook V2 semantic-graph commands: graph, artifacts, diff, activation, pending events, run overlays. | [cli/contracts.md](../cli/contracts.md) | `tests/test_playbook_v2_commands.py`, `tests/test_api_playbook_v2_commands.py` |
| [src/commands/plugin_commands.py](../../../src/commands/plugin_commands.py) | Implements the plugin lifecycle commands. | [cli/commands.md](../cli/commands.md) | `tests/test_plugin_commands.py` |
| [src/commands/profile_commands.py](../../../src/commands/profile_commands.py) | Implements agent-profile CRUD, export and import. | [cli/commands.md](../cli/commands.md) | `tests/test_agent_profiles.py`, `tests/test_profile_drift.py` |
| [src/commands/project_commands.py](../../../src/commands/project_commands.py) | Implements project CRUD, scheduling controls and workspace management. | [cli/commands.md](../cli/commands.md) | `tests/test_project_constraints.py` |
| [src/commands/project_onboarding_commands.py](../../../src/commands/project_onboarding_commands.py) | Implements the seven onboarding commands, parsing through the onboarding contract shapes. | [cli/contracts.md](../cli/contracts.md) | `tests/test_project_onboarding_commands.py` |
| [src/commands/proposal_commands.py](../../../src/commands/proposal_commands.py) | Implements the staged task-batch proposal flow. | [cli/commands.md](../cli/commands.md) | `tests/test_proposal_api.py`, `tests/test_proposal_status_event.py` |
| [src/commands/provider_commands.py](../../../src/commands/provider_commands.py) | Implements the provider quota probe. | [cli/commands.md](../cli/commands.md) | `tests/test_provider_usage_probe.py` |
| [src/commands/question_commands.py](../../../src/commands/question_commands.py) | Implements the worker-question commands, always taking actor identity from the server scope. | [cli/commands.md](../cli/commands.md) | `tests/test_agent_questions.py`, `tests/test_cli_questions.py` |
| [src/commands/routing_commands.py](../../../src/commands/routing_commands.py) | Implements the routing read: what a task needs and what could serve it. | [cli/commands.md](../cli/commands.md) | `tests/test_task_routing_contract.py`, `tests/test_routing_admission_v2.py` |
| [src/commands/session_commands.py](../../../src/commands/session_commands.py) | Implements the session commands and the daemon side of the completion protocol, including `task_close`. | [cli/commands.md](../cli/commands.md) | `tests/test_session_commands.py`, `tests/test_task_close_summary_enforcement.py` |
| [src/commands/spec_commands.py](../../../src/commands/spec_commands.py) | Implements the spec lifecycle commands. | [cli/commands.md](../cli/commands.md) | `tests/test_spec_approve_command.py` |
| [src/commands/surface_commands.py](../../../src/commands/surface_commands.py) | Implements the agent-facing context surface: `prime`, `get_schema`, `task_show`, `task_set`, `task_handoff`, `subagent_event`. | [cli/prime.md](../cli/prime.md) | `tests/test_surface_commands.py`, `tests/test_agent_subagents.py` |
| [src/commands/system_commands.py](../../../src/commands/system_commands.py) | Implements configuration, diagnostics, prompt management and orchestrator control. | [cli/commands.md](../cli/commands.md) | `tests/test_system_commands.py`, `tests/test_system_commands_prompt_path.py` |
| [src/commands/task_commands.py](../../../src/commands/task_commands.py) | Implements task CRUD, lifecycle, dependencies, hierarchy and plans — the largest mixin. | [cli/commands.md](../cli/commands.md) | `tests/test_command_surface.py`, `tests/test_get_downstream_tasks.py` |
| [src/commands/task_comment_commands.py](../../../src/commands/task_comment_commands.py) | Implements append-only authored task comments and their permissions. | [cli/commands.md](../cli/commands.md) | `tests/test_cli_task_comments.py` |
| [src/commands/tool_commands.py](../../../src/commands/tool_commands.py) | Implements `load_tools` and `find_applicable_tool`, filtering both by dispatchability and capability. | [cli/agent-tools.md](../cli/agent-tools.md) | `tests/test_tool_registry.py`, `tests/test_tool_index.py` |
| [src/commands/workflow_commands.py](../../../src/commands/workflow_commands.py) | Implements workflow CRUD and stage advancement. | [cli/commands.md](../cli/commands.md) | `tests/test_workflow_pipeline_view.py` |
| [src/commands/worktree_commands.py](../../../src/commands/worktree_commands.py) | Implements worktree-slot inspection and repair. | [cli/commands.md](../cli/commands.md) | `tests/test_worktree_doctor.py`, `tests/test_worktree_reaper.py` |

## Command contracts — `src/commands/contracts/`

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/commands/contracts/\_\_init\_\_.py](../../../src/commands/contracts/__init__.py) | Exposes the public contract boundary without registering anything at import time. | [cli/contracts.md](../cli/contracts.md) | Deferred registration breaks a circular import with the playbook explanation renderer. |
| [src/commands/contracts/models.py](../../../src/commands/contracts/models.py) | Defines the typed contract models, the effect clauses, the canonical document and the fingerprint. | [cli/contracts.md](../cli/contracts.md) | `tests/test_command_contracts_registry.py` |
| [src/commands/contracts/registry.py](../../../src/commands/contracts/registry.py) | Holds the process-wide registry, validates each registration and computes registry-wide fingerprints. | [cli/contracts.md](../cli/contracts.md) | `tests/test_command_contracts_registry.py` |
| [src/commands/contracts/builtin.py](../../../src/commands/contracts/builtin.py) | Declares the pipeline command contracts and the adapters that run them. | [cli/contracts.md](../cli/contracts.md) | `tests/test_command_contracts_registry.py` |
| [src/commands/contracts/integration.py](../../../src/commands/contracts/integration.py) | Declares the contracts for every hierarchical-integration primitive. | [cli/contracts.md](../cli/contracts.md) | `tests/test_integration_contracts.py` |
| [src/commands/contracts/escalation.py](../../../src/commands/contracts/escalation.py) | Declares the contracts for the durable human-escalation boundary. | [cli/contracts.md](../cli/contracts.md) | `tests/test_escalation_commands.py` |
| [src/commands/contracts/preview.py](../../../src/commands/contracts/preview.py) | Provides the side-effect-free preview seam for a future dry-run executor. | [cli/contracts.md](../cli/contracts.md) | No built-in registers a preview adapter today. |
| [src/commands/contracts/project_onboarding.py](../../../src/commands/contracts/project_onboarding.py) | Declares the request and response shapes and stable error codes for the seven onboarding commands. | [cli/contracts.md](../cli/contracts.md) | `tests/test_project_onboarding_contract.py`. Deliberately not in the fingerprinted registry. |

## Agent-facing tools — `src/tools/`

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/tools/\_\_init\_\_.py](../../../src/tools/__init__.py) | Re-exports the tool registry's public API and the definition tables. | [cli/agent-tools.md](../cli/agent-tools.md) | — |
| [src/tools/definitions.py](../../../src/tools/definitions.py) | Holds every authored tool definition and the tool-to-category mapping. | [cli/agent-tools.md](../cli/agent-tools.md) | `tests/test_tool_registry.py`. Pure data, so the CLI can generate its tree offline. |
| [src/tools/registry.py](../../../src/tools/registry.py) | Splits tools into core and on-demand categories, compresses schemas and merges plugin tools. | [cli/agent-tools.md](../cli/agent-tools.md) | `tests/test_tool_registry.py` |
| [src/tools/tool_index.py](../../../src/tools/tool_index.py) | Embeds tool names and descriptions in memory so `find_applicable_tool` can search them. | [cli/agent-tools.md](../cli/agent-tools.md) | `tests/test_tool_index.py`. Degrades to empty when no embedding provider is available. |

## Installer engine — `src/install/`

The shared orchestration behind `aq install`, plus the database adapter that
registers into it. Platform, packaging and provider adapters register steps the
same way rather than shipping installers of their own.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/install/\_\_init\_\_.py](../../../src/install/__init__.py) | Exposes the installer engine's public surface. | [cli/install.md](../cli/install.md) | — |
| [src/install/platform.py](../../../src/install/platform.py) | Detects the host — OS, version, architecture, distro, WSL generation — and places it in the supported-platform matrix. | [cli/install.md](../cli/install.md) | `tests/test_install_platform.py`. Every reader is injectable, so the whole matrix is testable on one box. |
| [src/install/results.py](../../../src/install/results.py) | Defines the four terminal step states, the run outcomes, the stable exit-code table and the machine-readable result. | [cli/install.md](../cli/install.md) | `tests/test_install_engine.py`. Exit codes are API: added, never reassigned. |
| [src/install/steps.py](../../../src/install/steps.py) | Declares the step protocol and the ordered, validated registry adapters register into. | [cli/install.md](../cli/install.md) | `tests/test_install_engine.py`. Cycles and mistyped dependencies fail at plan time. |
| [src/install/state.py](../../../src/install/state.py) | Reads and atomically writes the secret-free resume record, and refuses one written by another installer version. | [cli/install.md](../cli/install.md) | `tests/test_install_engine.py`. Mode `0600`; a leaked credential fails the write. |
| [src/install/redaction.py](../../../src/install/redaction.py) | Redacts secret-shaped keys and values, and fences the record against anything it missed. | [cli/install.md](../cli/install.md) | `tests/test_install_engine.py`. Reuses the denylist in `src/env_scrub.py`. |
| [src/install/engine.py](../../../src/install/engine.py) | Admits the host, orders and gates the steps, revalidates completed work instead of repeating it, collects consent, and stops at the first unsatisfied step. | [cli/install.md](../cli/install.md) | `tests/test_install_engine.py`. Clock-injectable; writes only the resume record. |
| [src/install/prerequisites.py](../../../src/install/prerequisites.py) | Supplies the engine's own steps: host admission, interpreter, Git, tmux and the AQ data directory. | [cli/install.md](../cli/install.md) | `tests/test_install_engine.py`. Detection only — installing anything belongs to an adapter. |
| [src/install/postgres.py](../../../src/install/postgres.py) | PostgreSQL mechanism: settings, connection classification, the administrator route, package and service plans, and the protected credential store. | [cli/install.md](../cli/install.md) | `tests/test_install_postgres.py`. Every host reader is injectable; passwords go to `psql` on stdin, never in argv. |
| [src/install/postgres_steps.py](../../../src/install/postgres_steps.py) | The nine `postgres.*` steps: install, start, admit, role, database, rotate, credentials, connection, boot. | [cli/install.md](../cli/install.md) | `tests/test_install_postgres.py`. Existing roles and databases are reused untouched; the schema stays the daemon's. |

## Startup context — `src/prime/`

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/prime/\_\_init\_\_.py](../../../src/prime/__init__.py) | Exposes the prime document model and renderer. | [cli/prime.md](../cli/prime.md) | — |
| [src/prime/models.py](../../../src/prime/models.py) | Defines the ordered ten-section document, its markdown assembly and its template variables. | [cli/prime.md](../cli/prime.md) | `tests/test_prime_renderer.py` |
| [src/prime/renderer.py](../../../src/prime/renderer.py) | Assembles the document for one task from the vault, the task rows and the workspace state. | [cli/prime.md](../cli/prime.md) | `tests/test_prime_renderer.py`, `tests/test_prime_session_workspace.py`. No LLM calls, no writes. |
| [src/prime/sections.py](../../../src/prime/sections.py) | Builds each individual section, including the message delivery marks and the capability-gated emergent-work block. | [cli/prime.md](../cli/prime.md) | `tests/test_prime_renderer.py` |
| [src/prime/overrides.py](../../../src/prime/overrides.py) | Loads `<work_dir>/.aq/PRIME.md` and substitutes its `{{token}}` variables. | [cli/prime.md](../cli/prime.md) | `tests/test_prime_renderer.py` |
| [src/prime/hook_envelopes.py](../../../src/prime/hook_envelopes.py) | Wraps a rendered body in a harness hook envelope, applies suppression, and parses sub-agent hook payloads. | [cli/prime.md](../cli/prime.md) | `tests/test_prime_hook_envelopes.py`, `tests/test_agent_subagents.py` |

## Top-level modules

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/surface_schema.py](../../../src/surface_schema.py) | Returns the code-owned enum catalog behind `aq schema` and `get_schema`, with no database or daemon access. | [cli/agent-tools.md](../cli/agent-tools.md) | `tests/test_surface_commands.py` |
| [src/known_tools.py](../../../src/known_tools.py) | Names the harness-provided tools a profile allowlist is written in, and parses a profile's install manifest. | [cli/agent-tools.md](../cli/agent-tools.md) | `tests/test_known_tools.py`. Validation is soft: unknown names warn. |

## Shipped prompt content

These files are not code: they are shipped text that reaches an agent's
context. They are covered where their behaviour is explained.

| File | Purpose | Component |
|---|---|---|
| [src/prime/templates/tool_guidance.md](../../../src/prime/templates/tool_guidance.md) | Tells a worker the surface is CLI-first, how to run tests, and which native tools exist. | [cli/prime.md](../cli/prime.md) |
| [src/prime/templates/completion_protocol.md](../../../src/prime/templates/completion_protocol.md) | Tells a worker how to close, what a deliverable self-check requires, and never to close over unpushed commits. | [cli/prime.md](../cli/prime.md) |
| [src/prime/templates/completion_protocol_pool.md](../../../src/prime/templates/completion_protocol_pool.md) | Adds the close-and-claim-next loop for a pool session. | [cli/prime.md](../cli/prime.md) |
| [src/prime/templates/emergent_work.md](../../../src/prime/templates/emergent_work.md) | Tells a worker to file discovered work instead of widening its own scope. | [cli/prime.md](../cli/prime.md) |
| [src/prime/templates/hooks/claude.json](../../../src/prime/templates/hooks/claude.json) | The Claude harness hook file: `SessionStart` on resume or compact, `PreCompact`, and both sub-agent events. | [cli/prime.md](../cli/prime.md) |
| [src/prime/templates/hooks/codex.json](../../../src/prime/templates/hooks/codex.json) | The Codex harness hook file: both sub-agent events. | [cli/prime.md](../cli/prime.md) |
| [src/skills/aq-cli/SKILL.md](../../../src/skills/aq-cli/SKILL.md) | Orients an agent in the command surface and how to get detail on a command. | [cli/agent-tools.md](../cli/agent-tools.md) |
| [src/skills/aq-tasks/SKILL.md](../../../src/skills/aq-tasks/SKILL.md) | Explains the task lifecycle from a worker's seat. | [cli/agent-tools.md](../cli/agent-tools.md) |
| [src/skills/aq-comms/SKILL.md](../../../src/skills/aq-comms/SKILL.md) | Explains messages, the inbox and reporting a blocker. | [cli/agent-tools.md](../cli/agent-tools.md) |
| [src/skills/aq-workspaces-and-git/SKILL.md](../../../src/skills/aq-workspaces-and-git/SKILL.md) | Explains the assigned worktree, its branch, and committing, pushing and opening a PR. | [cli/agent-tools.md](../cli/agent-tools.md) |
| [src/skills/aq-playbooks-and-gates/SKILL.md](../../../src/skills/aq-playbooks-and-gates/SKILL.md) | Explains inspecting a paused run, resolving a human gate, and what the shipped default pipeline does. | [cli/agent-tools.md](../cli/agent-tools.md) |

## Code-adjacent notes

| File | Purpose | Component |
|---|---|---|
| [src/cli/CLAUDE.md](../../../src/cli/CLAUDE.md) | The contributor-facing map of `src/cli/`, kept honest by `tests/test_cli_module_map.py`. | [cli/README.md](../cli/README.md) |

## Coverage

```bash
python3 docs/plans/documentation-overhaul/refresh_inventory.py --check
```

That command fails if a path in this shard's scope has no ownership rule, or
if the committed manifest no longer matches the tree. Every production module
the manifest assigns to the `cli` shard has a row above.
