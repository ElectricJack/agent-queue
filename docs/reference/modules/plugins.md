# Module catalog: plugins and MCP

This is the `plugins` shard of the [module catalog](README.md). It maps every
production module in this scope to the user-facing
[plugins, MCP, and extensions guide](../../guides/plugins-and-mcp.md).

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/aq_uri.py](../../../src/aq_uri.py) | Rewrites approved `aq://` resource references to config-rooted paths and rejects traversal. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#state-ownership) | `tests/test_aq_uri.py` |
| [src/embedded_mcp.py](../../../src/embedded_mcp.py) | Runs AQ's streamable-HTTP MCP server beside the FastAPI application and supervises restarts. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#configure-aqs-own-mcp-endpoint) | `tests/test_embedded_mcp.py` |
| [src/mcp_interfaces.py](../../../src/mcp_interfaces.py) | Defines MCP-facing resource and domain-model serialization types. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#two-mcp-directions) | MCP registration coverage |
| [src/mcp_registration.py](../../../src/mcp_registration.py) | Registers command, resource, and prompt surfaces as MCP capabilities and applies exclusions. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#configure-aqs-own-mcp-endpoint) | `tests/test_mcp_server.py` |
| [src/plugins/__init__.py](../../../src/plugins/__init__.py) | Exports the plugin authoring API and registry facade. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#build-a-minimal-plugin) | `tests/test_plugins.py` |
| [src/plugins/base.py](../../../src/plugins/base.py) | Defines plugin lifecycle classes, context API, trust levels, configuration, registrations, and cron decorator. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#build-a-minimal-plugin) | `tests/test_plugins.py`, `tests/test_plugin_external_service_access.py` |
| [src/plugins/loader.py](../../../src/plugins/loader.py) | Clones, installs, parses, and imports external plugin packages or legacy manifests. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#install-and-operate-plugins) | `tests/test_plugins.py` |
| [src/plugins/registry.py](../../../src/plugins/registry.py) | Owns plugin discovery, lifecycle, registrations, services, and failure isolation. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#state-ownership) | `tests/test_plugins.py`, `tests/test_plugin_service_registration.py` |
| [src/plugins/services.py](../../../src/plugins/services.py) | Declares typed service facades available to internal plugins. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#install-and-operate-plugins) | `tests/test_plugin_services.py` |
| [src/plugins/internal/__init__.py](../../../src/plugins/internal/__init__.py) | Discovers direct internal plugin modules and collects their tool and CLI formatter definitions. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | Direct-child discovery is intentional current behavior; nested inbox follow-up is tracked separately. |
| [src/plugins/internal/files.py](../../../src/plugins/internal/files.py) | Provides the shipped internal workspace file-operation commands and tool schemas. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | File command coverage is in the command/tool suites. |
| [src/plugins/internal/git.py](../../../src/plugins/internal/git.py) | Provides the shipped internal Git and integration command implementations and tool schemas. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | Git command coverage is in the command/tool suites. |
| [src/plugins/internal/notes.py](../../../src/plugins/internal/notes.py) | Provides the shipped internal project-note commands, tools, and CLI formatting. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | `tests/test_notes_plugin.py` |
| [src/plugins/internal/vibecop.py](../../../src/plugins/internal/vibecop.py) | Provides the shipped internal VibeCop static-analysis commands and tool schemas. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | `tests/test_vibecop_plugin.py` |
| [src/plugins/internal/inbox/__init__.py](../../../src/plugins/internal/inbox/__init__.py) | Marks the nested inbox implementation package. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | Source-present; not discovered by the current direct-module loader. |
| [src/plugins/internal/inbox/allowlist.py](../../../src/plugins/internal/inbox/allowlist.py) | Parses and reloads the per-project email sender allowlist and settings. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | `tests/test_inbox_plugin.py`; source-present, not loaded by current discovery. |
| [src/plugins/internal/inbox/auth.py](../../../src/plugins/internal/inbox/auth.py) | Evaluates email authentication results for inbox routing. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | `tests/test_inbox_plugin.py`; source-present, not loaded by current discovery. |
| [src/plugins/internal/inbox/gmail_client.py](../../../src/plugins/internal/inbox/gmail_client.py) | Wraps Gmail API authentication and message retrieval for the inbox implementation. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | `tests/test_inbox_plugin.py`; source-present, not loaded by current discovery. |
| [src/plugins/internal/inbox/plugin.py](../../../src/plugins/internal/inbox/plugin.py) | Defines the inbox poller plugin, its configuration, and administrative commands. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | `tests/test_inbox_plugin.py`; source-present, not loaded by current discovery. |
| [src/plugins/internal/inbox/poller.py](../../../src/plugins/internal/inbox/poller.py) | Polls Gmail and emits deterministic allowlisted or unknown-email events. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#shipped-and-external-plugin-status) | `tests/test_inbox_plugin.py`; source-present, not loaded by current discovery. |
| [src/profiles/mcp_catalog.py](../../../src/profiles/mcp_catalog.py) | Stores scoped external-MCP tool snapshots and resolves AQ's builtin tools in process. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#inputs-and-outputs) | `tests/test_mcp_catalog.py` |
| [src/profiles/mcp_inline_migration.py](../../../src/profiles/mcp_inline_migration.py) | Migrates legacy inline profile MCP configurations into named registry entries. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#configure-aqs-own-mcp-endpoint) | `tests/test_mcp_inline_migration.py` |
| [src/profiles/mcp_probe.py](../../../src/profiles/mcp_probe.py) | Probes stdio or HTTP MCP servers concurrently with a bounded timeout. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#common-failures-and-recovery) | `tests/test_mcp_probe.py` |
| [src/profiles/mcp_registry.py](../../../src/profiles/mcp_registry.py) | Parses vault-backed MCP definitions, applies scoped lookup, and watches registry changes. | [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md#a-realistic-setup-give-a-profile-an-external-tool-server) | `tests/test_mcp_registry.py`, `tests/test_mcp_commands.py` |

## Related pages

* [Plugins, MCP, and extensions](../../guides/plugins-and-mcp.md) — user setup,
  authoring, state ownership, and recovery.
* [Agent-facing tools](../cli/agent-tools.md) — shared tool publication and
  command authorization behavior.
* [Module catalog](README.md) — the index for all subsystem shards.
