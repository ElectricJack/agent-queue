"""Principal-aware resource authorization shared by dispatch and HTTP.

Capabilities select verbs, never resources. Rules here are deliberately explicit;
new generated handlers must be audited before their resource requirements are known.
The generated invocation release gate remains independent of this module.
"""
from __future__ import annotations


# These names operate on installation-wide state, even if they happen to accept
# project_id as a display filter. A filter is not proof of scoped execution.
SYSTEM_COMMANDS = frozenset("""
archive_settings claude_usage create_agent create_profile delete_agent delete_profile
edit_agent edit_profile export_profile get_config get_config_schema
get_github_auth_status get_profile get_status import_profile install_profile
list_github_owners list_intelligence_classes list_profiles list_project_roots
browse_project_root search_github_repositories onboard_project get_project_onboarding
bind_project_repository create_project delete_intelligence_class edit_intelligence_class
export_portable_config import_portable_config preview_portable_config update_config
reload_config migrate_profiles profile_audit profile_drift profile_reseed check_profile
list_available_tools list_mcp_tool_catalog list_prompts read_prompt plugin_config
plugin_disable plugin_enable plugin_info plugin_install plugin_list plugin_prompts
plugin_reload plugin_remove plugin_reset_prompts plugin_update policy_apply policy_diff
policy_export doctor doctor_run observation_snapshot_cleanup db_preflight_hierarchy digest_facts digest_post digest_preview
digest_status discord_purge_channel read_logs scan_stub_staleness vault_rebuild_index
list_projects list_active_tasks_all_projects orchestrator_control pool_status pool_scale pool_rename
pool_set_enabled pool_set_lifecycle provider_allocation_apply provider_history
provider_recheck provider_set_state provider_status provider_held_tasks
reconcile_agent_cron reconcile_agent_waits reconcile_collaborations cron_delivery_begin
cron_delivery_finish integration_train_tick job_reconcile job_submit_integration
observe_integration_source_ci queue_sync_workspaces session_cleanup supervisor_restart
start_agent_terminal session_input session_token
playbook_activation_health playbook_health list_playbooks list_playbook_runs
list_event_triggers playbook_pending_events playbook_pending_event_action
playbook_v2_import playbook_v2_validate playbook_v2_propose playbook_v2_shadow_compile
playbook_delete playbook_artifacts get_playbook_source update_playbook_source
set_playbook_enabled playbook_graph_layout_save playbook_v2_graph playbook_graph_view
show_playbook_graph playbook_artifact_diff
run_command shutdown restart_daemon update_and_restart
""".split())

# Handlers with no resource data (schemas/catalogues only).
PUBLIC_COMMANDS = frozenset("""
get_schema playbook_commands find_applicable_tool load_tools
formula_list formula_show list_workspace_kinds doctor_catalog
""".split())

# Session-lifecycle commands require an actual session. Held-work commands also
# re-derive the current claim/task/agent fence before touching their resources.
SESSION_COMMANDS = frozenset("""
prime subagent_event task_claim session_drain_ack operator_execute operator_preflight
""".split())
HELD_COMMANDS = frozenset("""
task_close task_heartbeat task_set task_comment task_comment_edit task_comment_delete
benchmark_stage_record artifact_verify object_checkpoint_read task_handoff
""".split())

# Each of these producers confines its primary query/mutation to project_id, or
# to the rows named by TARGET_RESOLVERS. Its own claim/review/CAS gates still run.
PROJECT_COMMANDS = frozenset("""
add_workspace advance_workflow_stage agent_message archive_task cancel_playbook_run get_agent list_agents
create_mcp_server probe_mcp_server create_task_graph create_workflow dashboard_state_get dashboard_state_list
dashboard_state_put dashboard_state_reset delete_mcp_server delete_project delete_task
edit_mcp_server edit_project edit_workspace explain_task find_merge_conflict_workspaces
formula_cook gate_list gate_show get_agent_error get_benchmark_report get_chain_health
get_chat_analyzer_metrics get_costs get_git_identity get_mcp_server get_project
get_recent_events get_task_dependencies get_task_result get_task_tree get_token_usage
get_workflow github_issue_close_rejected graph_layout_rebuild graph_reflow_status graph_tidy
inspect_playbook_run list_archived list_mcp_servers list_workflows list_workspaces
message_inbox message_list message_reply message_status pause_project pause_task phase_create
phase_list playbook_activate playbook_run_overlay pr_merge project_doctor project_ready
provide_input provider_reroute_undo question_answer question_escalate question_list release_project_constraint
release_workspace remove_dependency remove_task remove_workspace reopen_with_feedback
reparent_task restart_task resume_playbook resume_project resume_task review_attachment_add
review_attachment_list review_comment review_decide review_delegate review_dispatch
review_import_edits review_list review_reopen review_show review_submit review_withdraw
approve_pull_request run_playbook dry_run_playbook session_attach session_kill session_list
session_logs session_nudge session_peek session_prune session_show session_sleep session_wake
set_default_branch set_git_identity set_project_constraint set_task_status show_effective_profile
skip_task spec_approve task_batch_discard task_batch_propose task_batch_update task_children
task_comments task_deps task_progress task_recent_activity task_recover task_route
 task_route_override task_show task_subtask_add task_subtask_get task_subtask_update
 task_subtasks token_audit workflow_pipeline_view workspace_doctor workspace_reap
""".split())

# These commands already implement a nontrivial resource language: mailboxes,
# review packets/delegations, knowledge scope, graph-local IDs, or dashboard keys.
# Do not reinterpret value IDs as unrelated table IDs. Their independent handler
# gates are required, and plain project_id mismatches are still checked here.
HANDLER_RESOURCE_GATES = frozenset("""
message_send message_inbox message_list message_reply message_status message_wait
review_attachment_add review_attachment_list review_comment review_decide review_delegate
review_dispatch review_import_edits review_list review_reopen review_show review_submit
review_withdraw create_task_graph formula_cook task_batch_propose task_batch_update
dashboard_state_get dashboard_state_list dashboard_state_put dashboard_state_reset
""".split())

# Explicit read audit, not a command-name heuristic. Unannotated effects remain
# destructive and non-retryable; a read observation may still report unhealthy.
AUDITED_READS = frozenset("""
archive_settings check_profile claude_usage db_preflight_hierarchy doctor_catalog doctor_run digest_facts digest_preview
 digest_status explain_task find_applicable_tool find_merge_conflict_workspaces formula_list
 formula_show gate_list gate_show get_agent get_agent_error get_benchmark_report get_chain_health
get_chat_analyzer_metrics get_config get_config_schema get_costs get_git_identity
get_github_auth_status get_mcp_server get_playbook_source get_profile get_project
get_project_onboarding get_recent_events get_schema get_status get_task_dependencies
get_task_result get_task_tree get_token_usage get_workflow graph_reflow_status
inspect_playbook_run list_active_tasks_all_projects list_agents list_archived list_available_tools
list_event_triggers list_github_owners list_intelligence_classes list_mcp_servers
list_mcp_tool_catalog list_playbook_runs list_playbooks list_profiles list_project_roots
list_prompts list_workflows list_workspace_kinds list_workspaces message_inbox message_list
playbook_activation_health playbook_artifact_diff playbook_artifacts
playbook_commands playbook_graph_view playbook_health playbook_pending_events
playbook_run_overlay playbook_v2_graph plugin_info plugin_list plugin_prompts policy_diff
pool_status profile_audit profile_drift provider_held_tasks provider_history provider_status
question_list read_logs read_prompt review_attachment_list review_list review_show
session_list session_logs session_peek session_show show_effective_profile show_playbook_graph
 task_children task_comments task_deps task_progress task_recent_activity task_show
 task_subtask_get task_subtasks token_audit workflow_pipeline_view
""".split())


STATE_COMMANDS = frozenset({
    "playbook_state_get", "playbook_state_list", "playbook_state_set",
    "playbook_state_delete", "playbook_state_inspect", "playbook_state_reset",
    "playbook_state_notify",
})
STATE_PREVIEW_COMMANDS = STATE_COMMANDS - {"playbook_state_inspect", "playbook_state_reset"}
AUDITED_READS |= {"playbook_state_get", "playbook_state_list", "playbook_state_inspect"}


def resource_requirements(name: str) -> tuple[str, ...]:
    if name in STATE_COMMANDS:
        return ("invocation_namespace_or_operator",)
    if name in SESSION_COMMANDS:
        return ("live_session",)
    if name in HELD_COMMANDS:
        return ("held_session",)
    if name in SYSTEM_COMMANDS:
        return ("system_scope",)
    if name in PUBLIC_COMMANDS:
        return ("catalogue_only",)
    if name in PROJECT_COMMANDS | HANDLER_RESOURCE_GATES:
        return ("project_resources", "handler_gates")
    return ("resource_audit_required",)


def is_authorization_denial(result: dict) -> bool:
    """Recognize only published denial codes and the legacy scope prefix."""
    if result.get("success") is not False:
        return False
    if result.get("error_code") in {
        "unauthorized", "capability_denied", "out_of_scope", "resource_scope_denied",
        "playbook_command_denied", "server_owned_argument", "session_required",
        "not_decider", "not_your_task", "local_operator_only", "operator_only",
        "resource_audit_required",
    }:
        return True
    error = result.get("error")
    return isinstance(error, str) and error.startswith(("out of scope:", "capability denied:"))


def principal_scope(principal) -> dict | None:
    """Compatibility view derived from identity; never manufactures LOCAL scope."""
    if not principal.enforced:
        return None
    return {
        "kind": principal.kind.value, "session_id": principal.session_id,
        "session_instance_token": principal.session_instance_token,
        "task_id": principal.task_id, "project_id": principal.project_id,
        "elevated": principal.elevated,
    }


async def authorize_resources(
    command: str, args: dict, principal, *, db, generated=False, descriptor=None,
):
    """Return a safe refusal or None, before any handler or plugin dispatch."""
    from src.commands.descriptors import PLAYBOOK_DENIALS
    from src.commands.principal import PrincipalKind

    def deny(code="resource_scope_denied"):
        return {"success": False, "result": "out_of_scope", "error_code": code,
                "error": f"out of scope: {command}"}

    if principal.kind is PrincipalKind.PLAYBOOK and command in PLAYBOOK_DENIALS:
        return deny("playbook_command_denied")
    if not principal.enforced:
        return None
    if principal.kind is PrincipalKind.PLAYBOOK and command in {
        "review_decide", "review_delegate", "review_import_edits", "approve_pull_request",
        "promote_approve", "operator_execute", "operator_preflight",
    }:
        return deny("unauthorized")
    if command in SESSION_COMMANDS | HELD_COMMANDS and principal.kind is PrincipalKind.PLAYBOOK:
        return deny("session_required")
    if command == "message_send" and principal.kind is PrincipalKind.PLAYBOOK:
        # Shared submission validation resolves recipient ownership and derives
        # sender provenance. Authored sender fields cannot become resource authority.
        return None
    if command in STATE_COMMANDS:
        # This handler derives namespace membership from a pinned invocation;
        # no injected project argument or recursive interpretation of private values.
        return None
    project = principal.project_id
    if project is not None and args.get("project_id") not in (None, project):
        return deny()
    if project is not None and command in SYSTEM_COMMANDS:
        return deny()
    if project is None:
        if principal.kind is PrincipalKind.SESSION and not principal.elevated:
            if command not in PUBLIC_COMMANDS | SESSION_COMMANDS:
                return deny()
        return None
    # Unfiltered reads must inherit the caller's project. Typed contracts that
    # forbid project_id retain their row-based ownership check instead.
    if principal.kind is PrincipalKind.PLAYBOOK:
        properties = descriptor.input_schema().get("properties", {}) if descriptor else {}
        if command in PROJECT_COMMANDS | HANDLER_RESOURCE_GATES or "project_id" in properties:
            if args.get("project_id") is None:
                args["project_id"] = project
    # Graph-local identifiers and dashboard keys have dedicated independent
    # gates, including the complete nested graph and human-only state access.
    if command in {"create_task_graph", "formula_cook", "task_batch_propose", "task_batch_update"}:
        # Parent references are still checked below; nested IDs are local to
        # the validated change-set, whose writer checks every external edge.
        pass
    if command == "provider_reroute_undo":
        # Its batch is a provider sweep, not an integration batch. The service
        # confines the sweep using the original caller project.
        return None
    if command.startswith("dashboard_state_"):
        return None
    from src.api.target_scope import (
        CONTEXT_TARGET_RESOLVERS, TARGET_RESOLVERS, target_references, _target_project,
    )

    # Verify every recognized reference even when project_id was explicitly
    # supplied. A matching project filter cannot authorize a foreign task.
    references = target_references(args, command=command)
    if command.startswith("session_") and command not in {"session_list", "session_cleanup"}:
        # Session commands accept the CLI's id/name aliases. Canonicalize them
        # before checking ownership rather than trusting an injected filter.
        identity = args.get("session_id") or args.get("id") or args.get("name")
        if identity:
            references["session_id"] = [str(identity)]
    if command == "get_agent":
        # Agents are global identities; the scoped flock projection hides any
        # identity without an assignment/session visible in this project.
        references.pop("agent_id", None)
    # These tagged mailbox identities are not ordinary to_id/from_id rows.
    if command.startswith("message_"):
        for field in ("to_id", "from_id"):
            if field not in references:
                continue
            kind = args.get(field.replace("_id", "_kind"))
            if kind in {"task", "session"}:
                references.setdefault(kind + "_id", []).extend(references.pop(field))
            elif command == "message_list":
                # The query is already filtered by the inherited project.
                references.pop(field)
            elif (command == "message_inbox" and kind == "profile"
                  and principal.kind is PrincipalKind.SESSION):
                # The handler resolves the caller's live session profile and
                # enforces the mailbox fence, including same-project peers.
                references.pop(field)
            else:
                return deny()
        if (command == "message_inbox" and principal.kind is PrincipalKind.PLAYBOOK
                and args.get("to_kind") not in {"task", "session"}):
            return deny()
        # Thread IDs filter a project-bounded list; they grant no authority.
        if command == "message_list":
            references.pop("thread_id", None)
    # A run's event is a second resource input, not an authority override.
    if command in {"run_playbook", "dry_run_playbook"}:
        import json

        event = args.get("event") or {}
        if isinstance(event, str):
            try:
                event = json.loads(event)
            except (ValueError, TypeError):
                return deny()
        if not isinstance(event, dict):
            return deny()
        if event.get("project_id") not in (None, project):
            return deny()
        args["event"] = dict(event, project_id=project)
        for name, values in target_references(event).items():
            if name != "project_id":
                references.setdefault(name, []).extend(values)
        references.pop("playbook_id", None)  # activation lookup uses the inherited project
    if command == "playbook_activate" and args.get("artifact_sha256"):
        # The artifact owns the scope. The handler also checks that its declared
        # playbook_id matches the requested activation before any write.
        references.pop("playbook_id", None)

    for name, values in references.items():
        if name == "project_id":
            continue
        context_resolver = CONTEXT_TARGET_RESOLVERS.get((command, name))
        if name not in TARGET_RESOLVERS and context_resolver is None:
            # Existing typed commands retain their dedicated domain gates.
            # Opaque generated inputs may not conceal unreviewed references.
            if generated and principal.kind is PrincipalKind.PLAYBOOK:
                return deny("resource_audit_required")
            continue
        for value in values:
            try:
                found, owner = (await context_resolver(db, value, args) if context_resolver
                                else await _target_project(db, name, value))
            except Exception:
                return deny()
            if found and owner != project:
                if command in {"create_task_graph", "formula_cook"} and name == "parent_id":
                    refusal = deny()
                    refusal["error"] = "out of scope: hierarchy.parent_out_of_scope"
                    return refusal
                return deny()
            if not found and principal.kind is PrincipalKind.PLAYBOOK:
                return deny()
    return None
