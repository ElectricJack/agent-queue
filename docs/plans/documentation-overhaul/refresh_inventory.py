#!/usr/bin/env python3
"""Refresh the documentation-overhaul module inventory and ownership manifest.

This is the foundation ticket's coverage generator.  It reads the tracked file
list at the current implementation HEAD, assigns every tracked path to exactly
one documentation shard (an overhaul child ticket) and one owning documentation
page, and writes two artefacts next to this script:

``module-inventory.json``
    The refreshed inventory snapshot: tracked paths grouped by category.

``module-ownership.json``
    The manifest: one entry per tracked path with its shard, component page,
    catalog page and coverage mode.

Usage::

    python3 .../refresh_inventory.py                    # rewrite the artefacts
    python3 .../refresh_inventory.py --check            # coverage (every author)
    python3 .../refresh_inventory.py --check-artefacts  # freshness (acceptance)

The two halves of the check are deliberately separate, because they have
different owners.

``--check`` is the **coverage** half and is what every ticket runs before it
pushes.  It regenerates in memory and fails only when a tracked path matches no
rule.  That is the guarantee behind the foundation acceptance criterion "no
unassigned catch-all left unexplained": adding a new file under ``src/``
without an owning rule turns the check red.  When the committed artefacts have
fallen behind the tree it says so and still exits 0 — the paths that drifted
are printed with the shard they were assigned to, so an author can see where
their new file landed.

``--check-artefacts`` is the **freshness** half and is an acceptance gate, not
a per-ticket check.  It additionally fails when the committed JSON no longer
matches the tree, and the fix is to run the regenerator.  The artefacts are
owned by the overhaul's foundation/acceptance shard: they are one 3,700-entry
JSON pair, so twenty in-flight tickets each regenerating them on their own
branch would conflict with each other at delivery.  One owner regenerates on a
cadence instead.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

INVENTORY = HERE / "module-inventory.json"
OWNERSHIP = HERE / "module-ownership.json"

# --------------------------------------------------------------------------
# Shards.  One per overhaul child ticket that owns documentation pages.
# ``catalog`` is the module-coverage shard file the ticket writes; shards with
# no production modules of their own carry ``catalog: null``.
# --------------------------------------------------------------------------

SHARDS: dict[str, dict[str, str | None]] = {
    "foundation": {
        "title": "Documentation hierarchy, glossary and coverage manifest",
        "home": "docs/README.md",
        "catalog": None,
    },
    "quickstart": {
        "title": "Install and first task",
        "home": "docs/tutorials/README.md",
        "catalog": None,
    },
    "readme": {
        "title": "GitHub landing page",
        "home": "README.md",
        "catalog": None,
    },
    "architecture": {
        "title": "System architecture, startup and service boundaries",
        "home": "docs/concepts/architecture.md",
        "catalog": "docs/reference/modules/architecture.md",
    },
    "tasks": {
        "title": "Tasks, epics, dependencies, formulas and graph layout",
        "home": "docs/concepts/tasks.md",
        "catalog": "docs/reference/modules/tasks.md",
    },
    "scheduler": {
        "title": "Scheduling, worker pools and resource limits",
        "home": "docs/concepts/scheduling.md",
        "catalog": "docs/reference/modules/scheduler.md",
    },
    "routing": {
        "title": "Agents, profiles, intelligence classes and routing",
        "home": "docs/concepts/agents-and-routing.md",
        "catalog": "docs/reference/modules/routing.md",
    },
    "sessions": {
        "title": "Worker sessions, harnesses, terminals and claims",
        "home": "docs/concepts/sessions.md",
        "catalog": "docs/reference/modules/sessions.md",
    },
    "workspaces": {
        "title": "Projects, Git branches, workspaces and worktree slots",
        "home": "docs/concepts/projects-and-workspaces.md",
        "catalog": "docs/reference/modules/workspaces.md",
    },
    "integration": {
        "title": "Development integration and optional strict modes",
        "home": "docs/concepts/integration.md",
        "catalog": "docs/reference/modules/integration.md",
    },
    "playbooks": {
        "title": "Playbooks V2, events, gates and authoring",
        "home": "docs/concepts/playbooks.md",
        "catalog": "docs/reference/modules/playbooks.md",
    },
    "cli": {
        "title": "CLI, command layer, contracts and agent-facing tools",
        "home": "docs/reference/cli/README.md",
        "catalog": "docs/reference/modules/cli.md",
    },
    "api": {
        "title": "REST, WebSocket and generated clients",
        "home": "docs/reference/api/README.md",
        "catalog": "docs/reference/modules/api.md",
    },
    "dashboard": {
        "title": "Dashboard tour and frontend architecture",
        "home": "docs/guides/dashboard.md",
        "catalog": "docs/reference/modules/dashboard.md",
    },
    "database": {
        "title": "Database schema, queries, migrations and data lifecycle",
        "home": "docs/reference/database/README.md",
        "catalog": "docs/reference/modules/database.md",
    },
    "vault": {
        "title": "Configuration, vault, knowledge files and prompt assembly",
        "home": "docs/concepts/configuration-and-vault.md",
        "catalog": "docs/reference/modules/vault.md",
    },
    "providers": {
        "title": "LLM providers, usage accounting, tokens and budgets",
        "home": "docs/concepts/providers.md",
        "catalog": "docs/reference/modules/providers.md",
    },
    "plugins": {
        "title": "Plugins, MCP and extension development",
        "home": "docs/guides/plugins-and-mcp.md",
        "catalog": "docs/reference/modules/plugins.md",
    },
    "communications": {
        "title": "Messaging, Discord digests and escalations",
        "home": "docs/concepts/messaging.md",
        "catalog": "docs/reference/modules/communications.md",
    },
    "operations": {
        "title": "Diagnostics, observability, recovery and operation",
        "home": "docs/guides/operations.md",
        "catalog": "docs/reference/modules/operations.md",
    },
    "contributing": {
        "title": "Contributor setup, testing, builds and release tooling",
        "home": "docs/contributing/README.md",
        "catalog": "docs/reference/modules/contributing.md",
    },
    "reference": {
        "title": "Generated reference indexes and documentation checks",
        "home": "docs/reference/README.md",
        "catalog": None,
    },
    "legacy": {
        "title": "Existing documentation disposition and historical material",
        "home": "docs/history/README.md",
        "catalog": None,
    },
    "acceptance": {
        "title": "Final navigation assembly and coverage verification",
        "home": "docs/README.md",
        "catalog": None,
    },
}

# --------------------------------------------------------------------------
# Categories.
#   production  — ships in the running system; needs a named catalog entry.
#   supporting  — build, packaging, CI and developer tooling; documented by
#                 purpose in the contributing guides, not per symbol.
#   prompt      — shipped markdown/JSON the runtime reads at run time.
#   generated   — machine-generated; documented at resource-family level and
#                 never hand-edited.
#   documentation — prose; carries a disposition, not a catalog entry.
#   test        — tests and fixtures; documented as a layout, not per file.
# --------------------------------------------------------------------------

PRODUCTION = "production"
SUPPORTING = "supporting"
PROMPT = "prompt"
GENERATED = "generated"
DOCUMENTATION = "documentation"
TEST = "test"

# Pages the ownership table in docs/documentation-map.md assigns to a ticket.
# These are matched before the generic ``docs/**`` disposition rule, so a page
# an overhaul ticket writes is owned by that ticket rather than by ``legacy``.
# Every shard's ``home`` and ``catalog`` page is added automatically below.
OWNED_PAGES: list[tuple[str, str]] = [
    ("docs/documentation-map.md", "foundation"),
    ("docs/reference/glossary.md", "foundation"),
    ("docs/contributing/documentation-style.md", "foundation"),
    ("docs/reference/modules/README.md", "foundation"),
    ("docs/tutorials/**", "quickstart"),
    ("docs/reference/cli/**", "cli"),
    ("docs/reference/api/**", "api"),
    ("docs/reference/database/**", "database"),
    ("docs/reference/configuration.md", "vault"),
    ("docs/guides/worker-pools.md", "scheduler"),
    ("docs/guides/resource-gating.md", "scheduler"),
    ("docs/guides/project-onboarding.md", "workspaces"),
    ("docs/guides/development-integration.md", "integration"),
    ("docs/guides/dashboard.md", "dashboard"),
    ("docs/guides/plugins-and-mcp.md", "plugins"),
    ("docs/guides/escalations.md", "communications"),
    ("docs/guides/operations.md", "operations"),
    ("docs/contributing/**", "contributing"),
    ("docs/reference/README.md", "reference"),
    ("docs/reference/reference-maintenance.md", "reference"),
    ("docs/reference/configuration-schema.json", "reference"),
    ("docs/history/**", "legacy"),
]


def _owned_page_rules() -> list[tuple[str, str, str, str, str]]:
    """Turn the ownership table into rules, most specific first."""
    entries: dict[str, str] = {}
    for shard, meta in SHARDS.items():
        for key in ("home", "catalog"):
            page = meta[key]
            if page and page.startswith("docs/"):
                entries.setdefault(page, shard)
    for pattern, shard in OWNED_PAGES:
        entries[pattern] = shard
    rules = []
    for pattern, shard in entries.items():
        component = pattern if not pattern.endswith("/**") else pattern[:-3]
        rules.append(
            (
                pattern,
                shard,
                component,
                DOCUMENTATION,
                f"Overhaul page owned by the `{shard}` ticket.",
            ),
        )
    # Exact paths before directory globs so a shard home wins over its tree.
    rules.sort(key=lambda r: (r[0].endswith("/**"), -len(r[0])))
    return rules


# Ordered rules: (glob, shard, component page, category, note).
# First match wins, so narrower globs come first.
RULES: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------------------------- docs
    ("docs/plans/documentation-overhaul/*", "foundation",
     "docs/plans/documentation-overhaul/README.md", DOCUMENTATION,
     "This overhaul's own planning material."),
    ("docs/README.md", "foundation", "docs/README.md", DOCUMENTATION,
     "Documentation home; final navigation assembly owns it after foundation."),
    ("docs/reference/glossary.md", "foundation", "docs/reference/glossary.md",
     DOCUMENTATION, "Shared vocabulary."),
    ("docs/contributing/documentation-style.md", "foundation",
     "docs/contributing/documentation-style.md", DOCUMENTATION,
     "Style and runnable-example rules."),
    ("README.md", "readme", "README.md", DOCUMENTATION,
     "GitHub landing page."),
    *_owned_page_rules(),
    ("docs/**", "legacy", "docs/history/README.md", DOCUMENTATION,
     "Existing page; the legacy ticket records its disposition."),
    (".superpowers/**", "legacy", "docs/history/README.md", DOCUMENTATION,
     "Historical spec-driven-development evidence; preserved, not rewritten."),
    ("notes/**", "legacy", "docs/history/README.md", DOCUMENTATION,
     "Historical working notes."),
    ("reports/**", "legacy", "docs/history/README.md", DOCUMENTATION,
     "Historical reports."),
    ("vault/templates/**", "vault", "docs/concepts/configuration-and-vault.md",
     PROMPT, "Shipped vault template."),
    ("goals.md", "legacy", "docs/history/README.md", DOCUMENTATION, "Historical."),
    ("notes.md", "legacy", "docs/history/README.md", DOCUMENTATION, "Historical."),
    ("requirements.md", "legacy", "docs/history/README.md", DOCUMENTATION,
     "Historical."),
    ("breakdown.md", "legacy", "docs/history/README.md", DOCUMENTATION, "Historical."),
    ("test-coverage-impl-platform.md", "legacy", "docs/history/README.md",
     DOCUMENTATION, "Historical coverage report."),
    ("profile.md", "contributing", "docs/contributing/repo-map.md", DOCUMENTATION,
     "Repository architecture briefing loaded by agents."),
    ("CLAUDE.md", "contributing", "docs/contributing/repo-map.md", DOCUMENTATION,
     "Agent-facing repository instructions."),
    ("AGENTS.md", "contributing", "docs/contributing/repo-map.md", DOCUMENTATION,
     "Agent-facing repository instructions."),

    # ------------------------------------------------------------ generated
    ("packages/aq-client/**", "api", "docs/reference/api/python-client.md",
     GENERATED, "Generated from openapi.json; documented per resource family."),
    ("packages/aq-ts-client/**", "api", "docs/reference/api/typescript-client.md",
     GENERATED, "Generated TypeScript client package."),
    ("openapi.json", "api", "docs/reference/api/README.md", GENERATED,
     "Committed API schema artefact both clients generate from."),
    ("src/prompts/reviewed_playbooks/**", "playbooks",
     "docs/concepts/playbooks.md", GENERATED,
     "Reviewed playbook bundle: compiled artefact plus its digest."),

    # -------------------------------------------------------- shipped prompts
    ("src/prompts/default_playbooks/**", "playbooks",
     "docs/concepts/playbooks.md", PROMPT, "Shipped system-scope playbook source."),
    ("src/prompts/project_playbooks/**", "playbooks",
     "docs/concepts/playbooks.md", PROMPT, "Shipped project-scope playbook source."),
    ("src/prompts/default_intelligence_classes/**", "routing",
     "docs/concepts/agents-and-routing.md", PROMPT,
     "Shipped intelligence-class definition."),
    ("src/profiles/defaults/**", "routing", "docs/concepts/agents-and-routing.md",
     PROMPT, "Shipped agent profile definition."),
    ("src/prime/templates/**", "cli", "docs/reference/cli/prime.md", PROMPT,
     "Prime template rendered into a worker's opening context."),
    ("src/skills/**", "cli", "docs/reference/cli/agent-tools.md", PROMPT,
     "Shipped agent skill describing an aq surface."),
    ("src/prompts/*.md", "vault", "docs/concepts/configuration-and-vault.md",
     PROMPT, "Shipped prompt template."),
    ("src/playbook_schema.json", "playbooks", "docs/concepts/playbooks.md",
     SUPPORTING, "Playbook V1 JSON schema retained for validation."),
    ("src/playbook_v2_schema.json", "playbooks", "docs/concepts/playbooks.md",
     SUPPORTING, "Playbook V2 JSON schema."),
    ("src/cli/CLAUDE.md", "cli", "docs/reference/cli/README.md", DOCUMENTATION,
     "Code-adjacent CLI authoring notes."),
    ("dashboard/CLAUDE.md", "dashboard", "docs/guides/dashboard.md", DOCUMENTATION,
     "Code-adjacent frontend authoring notes."),

    # ----------------------------------------------------------- src: python
    ("src/__init__.py", "architecture", "docs/concepts/architecture.md",
     PRODUCTION, "Package root."),
    ("src/main.py", "architecture", "docs/concepts/architecture.md", PRODUCTION,
     "Daemon entry point: composes orchestrator, API, bot and MCP server."),
    ("src/_compat.py", "architecture", "docs/concepts/architecture.md", PRODUCTION,
     "Cross-version compatibility shims."),
    ("src/services/**", "architecture", "docs/concepts/architecture.md",
     PRODUCTION, "Service composition seam."),
    ("src/runtimes/**", "architecture", "docs/concepts/architecture.md",
     PRODUCTION, "Runtime ABC and registry; no in-tree implementations ship."),

    ("src/task_graph/layout/**", "tasks", "docs/concepts/tasks.md", PRODUCTION,
     "Graph spatial layout engine."),
    ("src/task_graph/**", "tasks", "docs/concepts/tasks.md", PRODUCTION,
     "Task graph parsing, validation, formulas and creation."),
    ("src/state_machine.py", "tasks", "docs/concepts/tasks.md", PRODUCTION,
     "Task status transitions."),
    ("src/deliverables.py", "tasks", "docs/concepts/tasks.md", PRODUCTION,
     "Deliverable declaration and close-time evidence checks."),
    ("src/task_names.py", "tasks", "docs/concepts/tasks.md", PRODUCTION,
     "Human-readable task identifiers."),
    ("src/task_summary.py", "tasks", "docs/concepts/tasks.md", PRODUCTION,
     "Task summary rendering."),
    ("src/explain.py", "tasks", "docs/concepts/tasks.md", PRODUCTION,
     "`aq task explain` — why a task is or is not runnable."),
    ("src/review_keys.py", "tasks", "docs/concepts/tasks.md", PRODUCTION,
     "Recognises a task whose work product is a review verdict."),

    ("src/orchestrator/workspace.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Workspace acquisition and release."),
    ("src/orchestrator/workspace_attachments.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Multi-kind workspace attachment."),
    ("src/orchestrator/workspace_claim_recovery.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Recovers workspaces stranded by a lost claim."),
    ("src/orchestrator/worktree_manager.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Git worktree slot lifecycle."),
    ("src/orchestrator/base_workspace.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Base repository checkout behind the slots."),
    ("src/orchestrator/git_ops.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Orchestrator-side Git operations."),
    ("src/orchestrator/**", "scheduler", "docs/concepts/scheduling.md", PRODUCTION,
     "Orchestrator cycle step."),
    ("src/scheduler.py", "scheduler", "docs/concepts/scheduling.md", PRODUCTION,
     "Pure pool sizing and placement."),
    ("src/schedule.py", "scheduler", "docs/concepts/scheduling.md", PRODUCTION,
     "Proportional project scheduling."),
    ("src/timer_service.py", "scheduler", "docs/concepts/scheduling.md", PRODUCTION,
     "Timer events that drive scheduled playbooks."),
    ("src/pool_claims.py", "scheduler", "docs/concepts/scheduling.md", PRODUCTION,
     "Pool claim bookkeeping."),
    ("src/resources/**", "scheduler", "docs/guides/resource-gating.md", PRODUCTION,
     "Per-session resource caps and the box-wide test slot semaphore."),

    ("src/profiles/mcp_registry.py", "plugins", "docs/guides/plugins-and-mcp.md",
     PRODUCTION, "In-memory MCP server registry backed by the vault."),
    ("src/profiles/mcp_probe.py", "plugins", "docs/guides/plugins-and-mcp.md",
     PRODUCTION, "Parallel MCP server probes."),
    ("src/profiles/mcp_catalog.py", "plugins", "docs/guides/plugins-and-mcp.md",
     PRODUCTION, "Probe result cache."),
    ("src/profiles/mcp_inline_migration.py", "plugins",
     "docs/guides/plugins-and-mcp.md", PRODUCTION,
     "Extracts legacy inline MCP definitions out of profiles."),
    ("src/profiles/workspace_kind_parser.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Parses workspace-kind markdown."),
    ("src/profiles/workspace_kind_registry.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Workspace-kind registry."),
    ("src/profiles/**", "routing", "docs/concepts/agents-and-routing.md",
     PRODUCTION, "Agent profile parsing, sync and migration."),
    ("src/agents/**", "routing", "docs/concepts/agents-and-routing.md", PRODUCTION,
     "Agent roster, liveness and terminal ownership."),
    ("src/intelligence_classes/**", "routing",
     "docs/concepts/agents-and-routing.md", PRODUCTION,
     "Intelligence-class parsing and live registry."),
    ("src/assignment_routing.py", "routing", "docs/concepts/agents-and-routing.md",
     PRODUCTION, "Routing state a task needs before a worker can take it."),
    ("src/agent_names.py", "routing", "docs/concepts/agents-and-routing.md",
     PRODUCTION, "Agent naming rules."),

    ("src/sessions/transcripts/**", "sessions", "docs/concepts/sessions.md",
     PRODUCTION, "Harness transcript readers."),
    ("src/sessions/**", "sessions", "docs/concepts/sessions.md", PRODUCTION,
     "Session lifecycle, harness launch and reconciliation."),
    ("src/panes/**", "sessions", "docs/concepts/sessions.md", PRODUCTION,
     "Terminal pane registry."),
    ("src/claim_file.py", "sessions", "docs/concepts/sessions.md", PRODUCTION,
     "`.aq/claim.json` read/write helpers."),
    ("src/env_scrub.py", "sessions", "docs/concepts/sessions.md", PRODUCTION,
     "Removes operator secrets from worker environments."),

    ("src/projects/**", "workspaces", "docs/concepts/projects-and-workspaces.md",
     PRODUCTION, "Project registration, onboarding and paths."),
    ("src/git/**", "workspaces", "docs/concepts/projects-and-workspaces.md",
     PRODUCTION, "Async Git manager, credentials and CI gate reads."),
    ("src/workspace_names.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Workspace naming rules."),
    ("src/workspace_spec_watcher.py", "workspaces",
     "docs/concepts/projects-and-workspaces.md", PRODUCTION,
     "Watches vault workspace-kind definitions."),

    ("src/integration/**", "integration", "docs/concepts/integration.md",
     PRODUCTION, "Source completion, validation, publication and recovery."),
    ("src/integrations/**", "integration", "docs/concepts/integration.md",
     PRODUCTION, "External integration adapters."),

    ("src/playbooks/executors/**", "playbooks", "docs/concepts/playbooks.md",
     PRODUCTION, "Playbook step executor."),
    ("src/playbooks/**", "playbooks", "docs/concepts/playbooks.md", PRODUCTION,
     "Playbook authoring, compilation, activation and run state."),
    ("src/event_bus.py", "playbooks", "docs/concepts/playbooks.md", PRODUCTION,
     "In-process event bus playbooks trigger from."),
    ("src/event_schemas.py", "playbooks", "docs/concepts/playbooks.md", PRODUCTION,
     "Event payload schemas."),
    ("src/workflow_pipeline_view.py", "playbooks", "docs/concepts/playbooks.md",
     PRODUCTION, "Pipeline projection of a multi-stage workflow."),
    ("src/workflow_stage_resume_handler.py", "playbooks",
     "docs/concepts/playbooks.md", PRODUCTION, "Resumes a paused workflow stage."),
    ("src/orphan_workflow_recovery.py", "playbooks", "docs/concepts/playbooks.md",
     PRODUCTION, "Recovers workflows whose owning run disappeared."),

    ("src/commands/contracts/**", "cli", "docs/reference/cli/contracts.md",
     PRODUCTION, "Command contract: the declared surface of one command."),
    ("src/commands/**", "cli", "docs/reference/cli/README.md", PRODUCTION,
     "Command handler module — the single entry point for every state change."),
    ("src/cli/**", "cli", "docs/reference/cli/README.md", PRODUCTION,
     "`aq` command-line surface."),
    ("src/tools/**", "cli", "docs/reference/cli/agent-tools.md", PRODUCTION,
     "Agent-facing tool definitions and registry."),
    ("src/prime/**", "cli", "docs/reference/cli/prime.md", PRODUCTION,
     "`aq prime` context assembly for a starting worker."),
    ("src/known_tools.py", "cli", "docs/reference/cli/agent-tools.md", PRODUCTION,
     "Catalogue of tool names the system recognises."),
    ("src/surface_schema.py", "cli", "docs/reference/cli/README.md", PRODUCTION,
     "`aq schema` — machine-readable command surface."),

    ("src/api/models/**", "api", "docs/reference/api/models.md", PRODUCTION,
     "Request/response model family."),
    ("src/api/routers/**", "api", "docs/reference/api/README.md", PRODUCTION,
     "Codegen router."),
    ("src/api/**", "api", "docs/reference/api/README.md", PRODUCTION,
     "HTTP or WebSocket surface module."),

    ("src/editor/**", "dashboard", "docs/guides/dashboard.md", PRODUCTION,
     "Backend half of the dashboard file editor."),

    ("src/database/queries/**", "database", "docs/reference/database/queries.md",
     PRODUCTION, "Query module: the durable reads and writes for one area."),
    ("src/database/adapters/**", "database", "docs/reference/database/README.md",
     PRODUCTION, "PostgreSQL adapter."),
    ("src/database/**", "database", "docs/reference/database/README.md",
     PRODUCTION, "Schema definition, engine and migration guard."),
    ("src/models.py", "database", "docs/reference/database/README.md", PRODUCTION,
     "Domain dataclasses shared across the system."),

    ("src/config.py", "vault", "docs/reference/configuration.md", PRODUCTION,
     "Configuration schema and loader."),
    ("src/config_editor.py", "vault", "docs/reference/configuration.md",
     PRODUCTION, "Round-trip configuration writer."),
    ("src/setup_wizard.py", "vault", "docs/reference/configuration.md", PRODUCTION,
     "First-run setup wizard."),
    ("src/vault*.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Vault storage, indexing and watching."),
    ("src/file_watcher.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Shared filesystem watcher."),
    ("src/wiki_links.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Wiki-link resolution inside vault markdown."),
    ("src/facts_handler.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Temporal facts read/write."),
    ("src/facts_parser.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Parses facts markdown."),
    ("src/prompt_builder.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Layered prompt assembly."),
    ("src/prompt_manager.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Prompt template storage."),
    ("src/override_handler.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Per-scope prompt overrides."),
    ("src/readme_handler.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Project README ingestion."),
    ("src/reference_stub_enricher.py", "vault",
     "docs/concepts/configuration-and-vault.md", PRODUCTION,
     "Fills reference stubs using the direct LLM path."),
    ("src/prompts/*.py", "vault", "docs/concepts/configuration-and-vault.md",
     PRODUCTION, "Prompt construction helper."),

    ("src/llm/providers/adapters/**", "providers", "docs/concepts/providers.md",
     PRODUCTION, "Provider wire-format adapter."),
    ("src/llm/**", "providers", "docs/concepts/providers.md", PRODUCTION,
     "Direct LLM call path."),
    ("src/providers/**", "providers", "docs/concepts/providers.md", PRODUCTION,
     "Provider usage observation and snapshots."),
    ("src/tokens/**", "providers", "docs/concepts/providers.md", PRODUCTION,
     "Token accounting and budgets."),
    ("src/llm_logger.py", "providers", "docs/concepts/providers.md", PRODUCTION,
     "Structured LLM call log."),

    ("src/plugins/internal/**", "plugins", "docs/guides/plugins-and-mcp.md",
     PRODUCTION, "Internal plugin shipped with the repository."),
    ("src/plugins/**", "plugins", "docs/guides/plugins-and-mcp.md", PRODUCTION,
     "Plugin base classes, registry and loader."),
    ("src/embedded_mcp.py", "plugins", "docs/guides/plugins-and-mcp.md",
     PRODUCTION, "Embedded MCP server exposing command-handler commands."),
    ("src/mcp_interfaces.py", "plugins", "docs/guides/plugins-and-mcp.md",
     PRODUCTION, "MCP protocol interfaces."),
    ("src/mcp_registration.py", "plugins", "docs/guides/plugins-and-mcp.md",
     PRODUCTION, "Registers commands as MCP tools."),
    ("src/aq_uri.py", "plugins", "docs/guides/plugins-and-mcp.md", PRODUCTION,
     "`aq://` resource URI parsing."),

    ("src/messages/**", "communications", "docs/concepts/messaging.md", PRODUCTION,
     "Message delivery to sessions, users and tasks."),
    ("src/messaging/**", "communications", "docs/concepts/messaging.md",
     PRODUCTION, "Transport-neutral messaging port."),
    ("src/notifications/**", "communications", "docs/concepts/messaging.md",
     PRODUCTION, "Notification event construction."),
    ("src/discord/**", "communications", "docs/concepts/messaging.md", PRODUCTION,
     "Discord gateway: digest delivery, escalation threads and intake."),
    ("src/digest/**", "communications", "docs/concepts/messaging.md", PRODUCTION,
     "Activity digest facts, eligibility, rendering and dispatch."),
    ("src/escalations/**", "communications", "docs/concepts/messaging.md",
     PRODUCTION, "Escalation incidents, delivery outbox and inbound intake."),

    ("src/doctor/**", "operations", "docs/guides/operations.md", PRODUCTION,
     "`aq doctor` check."),
    ("src/metrics/**", "operations", "docs/guides/operations.md", PRODUCTION,
     "Fleet metrics sampling."),
    ("src/logging_config.py", "operations", "docs/guides/operations.md",
     PRODUCTION, "Daemon logging configuration."),

    # ------------------------------------------------------------- dashboard
    ("dashboard/src/*.test.ts", "dashboard", "docs/guides/dashboard.md", TEST,
     "Frontend unit test."),
    ("dashboard/src/*.test.tsx", "dashboard", "docs/guides/dashboard.md", TEST,
     "Frontend unit test."),
    ("dashboard/src/testUtils/**", "dashboard", "docs/guides/dashboard.md", TEST,
     "Frontend test helper."),
    ("dashboard/src/setupTests.ts", "dashboard", "docs/guides/dashboard.md", TEST,
     "Vitest setup."),
    ("dashboard/src/api/**", "dashboard", "docs/guides/dashboard.md", PRODUCTION,
     "Typed dashboard API call."),
    ("dashboard/src/ws/**", "dashboard", "docs/guides/dashboard.md", PRODUCTION,
     "Dashboard WebSocket transport."),
    ("dashboard/src/shell/**", "dashboard", "docs/guides/dashboard.md", PRODUCTION,
     "Application shell: routing, layout and chrome."),
    ("dashboard/src/panes/**", "dashboard", "docs/guides/dashboard.md", PRODUCTION,
     "Dockable pane."),
    ("dashboard/src/pages/**", "dashboard", "docs/guides/dashboard.md", PRODUCTION,
     "Dashboard page or its supporting module."),
    ("dashboard/src/components/**", "dashboard", "docs/guides/dashboard.md",
     PRODUCTION, "Shared UI component."),
    ("dashboard/src/hooks/**", "dashboard", "docs/guides/dashboard.md", PRODUCTION,
     "Shared React hook."),
    ("dashboard/src/**", "dashboard", "docs/guides/dashboard.md", PRODUCTION,
     "Dashboard entry point or ambient declaration."),
    ("dashboard/scenarios/**", "dashboard", "docs/guides/dashboard.md", SUPPORTING,
     "Scenario harness used to capture dashboard states."),
    ("dashboard/public/**", "dashboard", "docs/guides/dashboard.md", SUPPORTING,
     "Static asset served with the dashboard."),
    ("dashboard/**", "dashboard", "docs/guides/dashboard.md", SUPPORTING,
     "Frontend build or dev-server configuration."),

    # ------------------------------------------------------- database schema
    ("migrations/**", "database", "docs/reference/database/migrations.md",
     SUPPORTING, "Alembic migration environment or revision."),
    ("alembic.ini", "database", "docs/reference/database/migrations.md",
     SUPPORTING, "Alembic configuration."),

    # ------------------------------------------------- contributor tooling
    ("tests/**", "contributing", "docs/contributing/testing.md", TEST,
     "Test module or fixture."),
    ("scripts/e2e/**", "contributing", "docs/contributing/testing.md", SUPPORTING,
     "End-to-end smoke driver."),
    ("scripts/**", "contributing", "docs/contributing/scripts.md", SUPPORTING,
     "Repository script."),
    (".github/workflows/**", "contributing", "docs/contributing/ci.md", SUPPORTING,
     "GitHub Actions workflow."),
    (".github/**", "contributing", "docs/contributing/ci.md", SUPPORTING,
     "GitHub configuration or example payload."),
    ("pyproject.toml", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "Python package and tooling configuration."),
    ("package.json", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "Repository-level npm manifest."),
    ("package-lock.json", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "Repository-level npm lockfile."),
    ("docker-compose.yml", "contributing", "docs/contributing/setup.md",
     SUPPORTING, "Local PostgreSQL service."),
    ("setup.sh", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "Installer script."),
    ("uninstall.sh", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "Uninstaller script."),
    ("run_tests.sh", "contributing", "docs/contributing/testing.md", SUPPORTING,
     "Test entry script."),
    ("test_suite.bat", "contributing", "docs/contributing/testing.md", SUPPORTING,
     "Windows test entry script."),
    (".pre-commit-config.yaml", "contributing", "docs/contributing/setup.md",
     SUPPORTING, "Pre-commit hook configuration."),
    (".vibecop.yml", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "Vibecop plugin configuration."),
    (".mcp.json", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "MCP servers offered to agents working in this repository."),
    ("mkdocs.yml", "legacy", "docs/history/README.md", SUPPORTING,
     "Legacy MkDocs configuration; the overhaul publishes GitHub-rendered Markdown."),
    (".gitignore", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "Ignore rules."),
    (".gitattributes", "contributing", "docs/contributing/setup.md", SUPPORTING,
     "Attribute rules."),
]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line)


def head_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
        check=True,
    ).stdout.strip()


def matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern)


def classify(path: str) -> tuple[str, str, str, str] | None:
    for pattern, shard, component, category, note in RULES:
        if matches(path, pattern):
            return shard, component, category, note
    return None


def build() -> tuple[dict, dict, list[str]]:
    paths = tracked_files()
    ownership: dict[str, dict[str, str | None]] = {}
    unassigned: list[str] = []

    for path in paths:
        hit = classify(path)
        if hit is None:
            unassigned.append(path)
            continue
        shard, component, category, note = hit
        entry: dict[str, str | None] = {
            "shard": shard,
            "component": component,
            "category": category,
            "purpose": note,
        }
        catalog = SHARDS[shard]["catalog"]
        entry["catalog"] = catalog if category == PRODUCTION else None
        ownership[path] = entry

    by_category: dict[str, list[str]] = {}
    for path, entry in ownership.items():
        by_category.setdefault(str(entry["category"]), []).append(path)
    for value in by_category.values():
        value.sort()

    commit = head_commit()

    inventory = {
        "source_commit": commit,
        "generated_by": "docs/plans/documentation-overhaul/refresh_inventory.py",
        "note": (
            "Refreshed against implementation HEAD by the foundation ticket. "
            "Every tracked path is assigned an owning shard in "
            "module-ownership.json; production entries additionally name the "
            "module-catalog shard that must carry a named entry for them."
        ),
        "counts": {k: len(v) for k, v in sorted(by_category.items())},
        "production_module_candidates": by_category.get(PRODUCTION, []),
        "generated_modules": by_category.get(GENERATED, []),
        "shipped_prompts": by_category.get(PROMPT, []),
        "supporting_files": by_category.get(SUPPORTING, []),
        "documentation_files": by_category.get(DOCUMENTATION, []),
        "test_files": by_category.get(TEST, []),
    }

    manifest = {
        "source_commit": commit,
        "generated_by": "docs/plans/documentation-overhaul/refresh_inventory.py",
        "note": (
            "Assignment, not authored coverage.  Each entry's `purpose` is the "
            "rule-level description of the family the path belongs to; the "
            "per-module sentence is written by the owning shard's catalog page "
            "(`shards[<shard>].catalog`).  `component` is the prose page that "
            "explains the module.  There is no catch-all rule: a tracked path "
            "matching nothing is an error, not a default assignment."
        ),
        "shards": SHARDS,
        "categories": {
            PRODUCTION: "Ships in the running system; needs a named catalog entry.",
            GENERATED: "Machine-generated; covered at resource-family level, never hand-edited.",
            PROMPT: "Shipped markdown or JSON the runtime reads at run time.",
            SUPPORTING: "Build, packaging, CI or developer tooling; documented by purpose.",
            DOCUMENTATION: "Prose; carries a disposition rather than a catalog entry.",
            TEST: "Tests and fixtures; documented as a layout, not per file.",
        },
        "modules": dict(sorted(ownership.items())),
    }

    return inventory, manifest, unassigned


def write(obj: dict, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _rel(path: Path) -> Path:
    """``path`` relative to the repository root, or unchanged when outside it."""
    try:
        return path.relative_to(REPO)
    except ValueError:
        return path


def _load(path: Path) -> dict | None:
    """Read a committed artefact, or ``None`` when it is missing or unreadable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def stale_artefacts(inventory: dict, manifest: dict) -> list[Path]:
    """Return the committed artefacts that no longer match the tree.

    ``source_commit`` is provenance, not content: it changes with every commit,
    so comparing it would make the check red the moment the regenerated
    artefacts are committed.  Only the assignments are compared.
    """
    stale: list[Path] = []
    for obj, path in ((inventory, INVENTORY), (manifest, OWNERSHIP)):
        have = _load(path)
        if have is None:
            stale.append(_rel(path))
            continue
        want = dict(obj)
        want.pop("source_commit", None)
        have.pop("source_commit", None)
        if have != want:
            stale.append(_rel(path))
    return stale


def drift(manifest: dict) -> list[tuple[str, str, str]]:
    """Paths the tree has gained, lost or re-assigned since the last rewrite.

    Each entry is ``(sign, path, shard)`` with ``sign`` one of ``+``, ``-`` or
    ``~``.  Returns an empty list when the committed manifest is unreadable —
    that is reported as staleness, not as drift.
    """
    have = _load(OWNERSHIP)
    if have is None:
        return []
    before: dict[str, dict] = have.get("modules", {})
    after: dict[str, dict] = manifest["modules"]
    out: list[tuple[str, str, str]] = []
    for path in sorted(set(after) - set(before)):
        out.append(("+", path, str(after[path]["shard"])))
    for path in sorted(set(before) - set(after)):
        out.append(("-", path, str(before[path].get("shard", "?"))))
    for path in sorted(set(before) & set(after)):
        if before[path] != after[path]:
            out.append(("~", path, str(after[path]["shard"])))
    return out


def report_drift(entries: list[tuple[str, str, str]], recorded: str | None) -> None:
    at = f" at {recorded[:12]}" if recorded else ""
    print(
        f"note: {len(entries)} tracked path(s) have changed since the coverage "
        f"manifest was regenerated{at}:",
    )
    for sign, path, shard in entries[:20]:
        print(f"  {sign} {path}  →  shard {shard}")
    if len(entries) > 20:
        print(f"  ... and {len(entries) - 20} more")
    print(
        "The manifest is owned by the overhaul's foundation/acceptance shard. "
        "Do not\nregenerate it on a ticket branch — twenty branches each "
        "rewriting a 3,700-entry\nJSON all conflict at delivery. Freshness is "
        "an acceptance gate: --check-artefacts.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="verify coverage: every tracked path has an owning rule",
    )
    parser.add_argument(
        "--check-artefacts", action="store_true",
        help=(
            "acceptance gate: coverage, plus the committed artefacts being "
            "current with the tree"
        ),
    )
    args = parser.parse_args()

    inventory, manifest, unassigned = build()

    if unassigned:
        print(f"{len(unassigned)} tracked path(s) have no documentation owner:")
        for path in unassigned[:40]:
            print(f"  {path}")
        if len(unassigned) > 40:
            print(f"  ... and {len(unassigned) - 40} more")
        print(
            "\nAdd a rule to RULES in "
            "docs/plans/documentation-overhaul/refresh_inventory.py.",
        )
        return 1

    if args.check or args.check_artefacts:
        recorded = (_load(OWNERSHIP) or {}).get("source_commit")
        stale = stale_artefacts(inventory, manifest)
        entries = drift(manifest)

        if args.check_artefacts and stale:
            print("stale artefact(s): " + ", ".join(str(p) for p in stale))
            if entries:
                report_drift(entries, recorded)
            print("run: python3 docs/plans/documentation-overhaul/refresh_inventory.py")
            return 1

        print(
            f"ok — {sum(inventory['counts'].values())} tracked paths assigned "
            f"({inventory['counts'].get('production', 0)} production modules)",
        )
        if args.check_artefacts:
            if recorded and recorded != head_commit():
                print(f"note: artefacts last regenerated at {recorded[:12]}")
            return 0
        if stale and entries:
            report_drift(entries, recorded)
        elif stale:
            print(
                "note: the committed artefacts are stale and are regenerated by "
                "the foundation/acceptance shard (--check-artefacts).",
            )
        return 0

    write(inventory, INVENTORY)
    write(manifest, OWNERSHIP)
    print(f"wrote {INVENTORY.relative_to(REPO)} and {OWNERSHIP.relative_to(REPO)}")
    for name, count in sorted(inventory["counts"].items()):
        print(f"  {name:<14} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
