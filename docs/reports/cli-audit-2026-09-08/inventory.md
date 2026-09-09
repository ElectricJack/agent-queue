# CLI command inventory — 2026-09-08

All 318 core leaf commands were discovered. Help was checked for every entry; `aq test` uses `--aq-help`. Installed plugin-specific top-level extensions were excluded from this offline inventory. A missing core handler does not imply a defect: file, git, note, and memory commands use runtime plugins. Live verification means only the listed read operation was exercised, not every flag or state.

| Command | Evidence / status | Backend | Owner |
|---|---|---|---|
| `aq` | Help verified | handwritten | handwritten/unimplemented |
| `aq agent` | Help verified | handwritten | handwritten/unimplemented |
| `aq agent check-profile` | Mock dispatch verified; live operation untested | check_profile | core |
| `aq agent create` | Mock dispatch verified; live operation untested | create_agent | core |
| `aq agent create-profile` | Mock dispatch verified; live operation untested | create_profile | core |
| `aq agent delete` | Mock dispatch verified; live operation untested | delete_agent | core |
| `aq agent delete-profile` | Mock dispatch verified; live operation untested | delete_profile | core |
| `aq agent edit` | Mock dispatch verified; live operation untested | edit_agent | core |
| `aq agent edit-profile` | Mock dispatch verified; live operation untested | edit_profile | core |
| `aq agent export-profile` | Mock dispatch verified; live operation untested | export_profile | core |
| `aq agent get` | Mock dispatch verified; live operation untested | get_agent | core |
| `aq agent get-error` | Mock dispatch verified; live operation untested | get_agent_error | core |
| `aq agent get-profile` | Mock dispatch verified; live operation untested | get_profile | core |
| `aq agent import-profile` | Mock dispatch verified; live operation untested | import_profile | core |
| `aq agent install-profile` | Mock dispatch verified; live operation untested | install_profile | core |
| `aq agent list` | Live read verified; JSON format varies | list_agents | core |
| `aq agent list-available-tools` | Mock dispatch verified; live operation untested | list_available_tools | core |
| `aq agent list-profiles` | Live read verified; JSON format varies | list_profiles | core |
| `aq agent message` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq agent profile-audit` | Mock dispatch verified; live operation untested | profile_audit | core |
| `aq agent profile-drift` | Mock dispatch verified; live operation untested | profile_drift | core |
| `aq agent profile-reseed` | Mock dispatch verified; live operation untested | profile_reseed | core |
| `aq agent show-effective-profile` | Mock dispatch verified; live operation untested | show_effective_profile | core |
| `aq agent start-terminal` | Mock dispatch verified; live operation untested | start_agent_terminal | core |
| `aq chat` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq costs` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq db` | Help verified | handwritten | handwritten/unimplemented |
| `aq db current` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq db import-sqlite` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq db upgrade` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq discord` | Help verified | handwritten | handwritten/unimplemented |
| `aq discord cleanup-threads` | Mock dispatch verified; live operation untested | discord_cleanup_threads | core |
| `aq discord purge-channel` | Mock dispatch verified; live operation untested | discord_purge_channel | core |
| `aq doctor` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq file` | Help verified | handwritten | handwritten/unimplemented |
| `aq file count-project-memory` | Mock dispatch verified; live operation untested | count_project_memory_files | runtime plugin |
| `aq file edit` | Mock dispatch verified; live operation untested | edit_file | runtime plugin |
| `aq file glob` | Mock dispatch verified; live operation untested | glob_files | runtime plugin |
| `aq file grep` | Mock dispatch verified; live operation untested | grep | runtime plugin |
| `aq file list-directory` | Live read verified; JSON format varies | list_directory | runtime plugin |
| `aq file read` | Mock dispatch verified; live operation untested | read_file | runtime plugin |
| `aq file read-project-memory` | Mock dispatch verified; live operation untested | read_project_memory_file | runtime plugin |
| `aq file record-inspection` | Mock dispatch verified; live operation untested | record_file_inspection | runtime plugin |
| `aq file search` | Mock dispatch verified; live operation untested | search_files | runtime plugin |
| `aq file select-for-inspection` | Mock dispatch verified; live operation untested | select_files_for_inspection | runtime plugin |
| `aq file write` | Mock dispatch verified; live operation untested | write_file | runtime plugin |
| `aq formula` | Help verified | handwritten | handwritten/unimplemented |
| `aq formula cook` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq formula list` | Live read verified; JSON format varies | formula_list | core |
| `aq formula show` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq git` | Help verified | handwritten | handwritten/unimplemented |
| `aq git branch` | Mock dispatch verified; live operation untested | git_branch | runtime plugin |
| `aq git changed-files` | Mock dispatch verified; live operation untested | git_changed_files | runtime plugin |
| `aq git checkout` | Mock dispatch verified; live operation untested | git_checkout | runtime plugin |
| `aq git checkout-branch` | Mock dispatch verified; live operation untested | checkout_branch | runtime plugin |
| `aq git ci-baseline-status` | Mock dispatch verified; live operation untested | ci_baseline_status | core |
| `aq git commit` | Mock dispatch verified; live operation untested | git_commit | runtime plugin |
| `aq git commit-changes` | Mock dispatch verified; live operation untested | commit_changes | runtime plugin |
| `aq git create-branch` | Mock dispatch verified; live operation untested | create_branch | runtime plugin |
| `aq git create-github-repo` | Mock dispatch verified; live operation untested | create_github_repo | runtime plugin |
| `aq git create-pr` | Mock dispatch verified; live operation untested | git_create_pr | runtime plugin |
| `aq git diff` | Mock dispatch verified; live operation untested | git_diff | runtime plugin |
| `aq git generate-readme` | Mock dispatch verified; live operation untested | generate_readme | runtime plugin |
| `aq git get-status` | Live read verified; JSON format varies | get_git_status | runtime plugin |
| `aq git log` | Mock dispatch verified; live operation untested | git_log | runtime plugin |
| `aq git merge` | Mock dispatch verified; live operation untested | git_merge | runtime plugin |
| `aq git merge-branch` | Mock dispatch verified; live operation untested | merge_branch | runtime plugin |
| `aq git pr-merge` | Mock dispatch verified; live operation untested | pr_merge | core |
| `aq git pull` | Mock dispatch verified; live operation untested | git_pull | runtime plugin |
| `aq git push` | Mock dispatch verified; live operation untested | git_push | runtime plugin |
| `aq git push-branch` | Mock dispatch verified; live operation untested | push_branch | runtime plugin |
| `aq git remote-url` | Mock dispatch verified; live operation untested | git_remote_url | runtime plugin |
| `aq graph` | Help verified | handwritten | handwritten/unimplemented |
| `aq graph layout-rebuild` | Mock dispatch verified; live operation untested | graph_layout_rebuild | core |
| `aq graph tidy` | Mock dispatch verified; live operation untested | graph_tidy | core |
| `aq handoff` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq inbox` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq integration` | Help verified | handwritten | handwritten/unimplemented |
| `aq integration abort` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq integration enable` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq integration flush` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq integration resume` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq integration retry-cleanup` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq integration status` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq integration waive-history` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq logs` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq mcp` | Help verified | handwritten | handwritten/unimplemented |
| `aq mcp create-server` | Mock dispatch verified; live operation untested | create_mcp_server | core |
| `aq mcp delete-server` | Mock dispatch verified; live operation untested | delete_mcp_server | core |
| `aq mcp edit-server` | Mock dispatch verified; live operation untested | edit_mcp_server | core |
| `aq mcp get-server` | Mock dispatch verified; live operation untested | get_mcp_server | core |
| `aq mcp list-servers` | Live read verified; JSON format varies | list_mcp_servers | core |
| `aq mcp list-tool-catalog` | Live read verified; JSON format varies | list_mcp_tool_catalog | core |
| `aq mcp probe-server` | Mock dispatch verified; live operation untested | probe_mcp_server | core |
| `aq memory` | Help verified | handwritten | handwritten/unimplemented |
| `aq memory save` | Mock dispatch verified; live operation untested | memory_save | runtime plugin |
| `aq memory search` | Mock dispatch verified; live operation untested | memory_search | runtime plugin |
| `aq message` | Help verified | handwritten | handwritten/unimplemented |
| `aq message inbox` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq message list` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq message reply` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq message send` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq message status` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq note` | Help verified | handwritten | handwritten/unimplemented |
| `aq note append` | Mock dispatch verified; live operation untested | append_note | runtime plugin |
| `aq note compare-specs` | Mock dispatch verified; live operation untested | compare_specs_notes | runtime plugin |
| `aq note delete` | Mock dispatch verified; live operation untested | delete_note | runtime plugin |
| `aq note list` | Live read verified; JSON format varies | list_notes | runtime plugin |
| `aq note promote` | Mock dispatch verified; live operation untested | promote_note | runtime plugin |
| `aq note read` | Mock dispatch verified; live operation untested | read_note | runtime plugin |
| `aq note write` | Mock dispatch verified; live operation untested | write_note | runtime plugin |
| `aq playbook` | Help verified | handwritten | handwritten/unimplemented |
| `aq playbook activate` | Mock dispatch verified; live operation untested | playbook_activate | core |
| `aq playbook activation-health` | Mock dispatch verified; live operation untested | playbook_activation_health | core |
| `aq playbook artifact-diff` | Mock dispatch verified; live operation untested | playbook_artifact_diff | core |
| `aq playbook artifacts` | Mock dispatch verified; live operation untested | playbook_artifacts | core |
| `aq playbook cancel-run` | Mock dispatch verified; live operation untested | cancel_playbook_run | core |
| `aq playbook delete` | Mock dispatch verified; live operation untested | playbook_delete | core |
| `aq playbook dry-run` | Mock dispatch verified; live operation untested | dry_run_playbook | core |
| `aq playbook get-source` | Mock dispatch verified; live operation untested | get_playbook_source | core |
| `aq playbook graph-layout-save` | Mock dispatch verified; live operation untested | playbook_graph_layout_save | core |
| `aq playbook graph-view` | Mock dispatch verified; live operation untested | playbook_graph_view | core |
| `aq playbook health` | Live read verified; JSON format varies | playbook_health | core |
| `aq playbook inspect-run` | Mock dispatch verified; live operation untested | inspect_playbook_run | core |
| `aq playbook list` | Live read verified; JSON format varies | list_playbooks | core |
| `aq playbook list-runs` | Live read verified; JSON format varies | list_playbook_runs | core |
| `aq playbook pending-event-action` | Mock dispatch verified; live operation untested | playbook_pending_event_action | core |
| `aq playbook pending-events` | Mock dispatch verified; live operation untested | playbook_pending_events | core |
| `aq playbook resume` | Mock dispatch verified; live operation untested | resume_playbook | core |
| `aq playbook run` | Mock dispatch verified; live operation untested | run_playbook | core |
| `aq playbook run-overlay` | Mock dispatch verified; live operation untested | playbook_run_overlay | core |
| `aq playbook set-enabled` | Mock dispatch verified; live operation untested | set_playbook_enabled | core |
| `aq playbook show-graph` | Mock dispatch verified; live operation untested | show_playbook_graph | core |
| `aq playbook update-source` | Mock dispatch verified; live operation untested | update_playbook_source | core |
| `aq playbook v2-graph` | Mock dispatch verified; live operation untested | playbook_v2_graph | core |
| `aq playbook v2-import` | Mock dispatch verified; live operation untested | playbook_v2_import | core |
| `aq playbook v2-propose` | Mock dispatch verified; live operation untested | playbook_v2_propose | core |
| `aq playbook v2-shadow-compile` | Mock dispatch verified; live operation untested | playbook_v2_shadow_compile | core |
| `aq playbook v2-validate` | Mock dispatch verified; live operation untested | playbook_v2_validate | core |
| `aq plugin` | Help verified | handwritten | handwritten/unimplemented |
| `aq plugin config` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin diff-prompts` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin disable` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin enable` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin info` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin install` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin list` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin logs` | Obsolete: hook-history stub | handwritten | handwritten/unimplemented |
| `aq plugin prompts` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin reload` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin remove` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin reset-prompts` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq plugin update` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq pool` | Help verified | handwritten | handwritten/unimplemented |
| `aq pool scale` | Mock dispatch verified; live operation untested | pool_scale | core |
| `aq pool set-enabled` | Mock dispatch verified; live operation untested | pool_set_enabled | core |
| `aq pool set-lifecycle` | Mock dispatch verified; live operation untested | pool_set_lifecycle | core |
| `aq pool status` | Live read verified; JSON format varies | pool_status | core |
| `aq prime` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq project` | Help verified | handwritten | handwritten/unimplemented |
| `aq project add-workspace` | Mock dispatch verified; live operation untested | add_workspace | core |
| `aq project browse-root` | Mock dispatch verified; live operation untested | browse_project_root | core |
| `aq project create` | Mock dispatch verified; live operation untested | create_project | core |
| `aq project delete` | Mock dispatch verified; live operation untested | delete_project | core |
| `aq project details` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq project edit-workspace` | Mock dispatch verified; live operation untested | edit_workspace | core |
| `aq project find-merge-conflict-workspaces` | Mock dispatch verified; live operation untested | find_merge_conflict_workspaces | core |
| `aq project get` | Mock dispatch verified; live operation untested | get_project | core |
| `aq project get-channels` | Mock dispatch verified; live operation untested | get_project_channels | core |
| `aq project get-for-channel` | Mock dispatch verified; live operation untested | get_project_for_channel | core |
| `aq project get-github-auth-status` | Mock dispatch verified; live operation untested | get_github_auth_status | core |
| `aq project get-onboarding` | Mock dispatch verified; live operation untested | get_project_onboarding | core |
| `aq project list` | Live read verified; JSON format varies | list_projects | core |
| `aq project list-github-owners` | Mock dispatch verified; live operation untested | list_github_owners | core |
| `aq project list-roots` | Live read verified; JSON format varies | list_project_roots | core |
| `aq project list-workspace-kinds` | Live read verified; JSON format varies | list_workspace_kinds | core |
| `aq project list-workspaces` | Live read verified; JSON format varies | list_workspaces | core |
| `aq project onboard` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq project pause` | Mock dispatch verified; live operation untested | pause_project | core |
| `aq project queue-sync-workspaces` | Mock dispatch verified; live operation untested | queue_sync_workspaces | core |
| `aq project ready` | Mock dispatch verified; live operation untested | project_ready | core |
| `aq project release-constraint` | Mock dispatch verified; live operation untested | release_project_constraint | core |
| `aq project release-workspace` | Mock dispatch verified; live operation untested | release_workspace | core |
| `aq project remove-workspace` | Mock dispatch verified; live operation untested | remove_workspace | core |
| `aq project resume` | Mock dispatch verified; live operation untested | resume_project | core |
| `aq project search-github-repositories` | Mock dispatch verified; live operation untested | search_github_repositories | core |
| `aq project set` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq project set-constraint` | Mock dispatch verified; live operation untested | set_project_constraint | core |
| `aq project set-control-interface` | Mock dispatch verified; live operation untested | set_control_interface | core |
| `aq project workspace-doctor` | Mock dispatch verified; live operation untested | workspace_doctor | core |
| `aq project workspace-reap` | Mock dispatch verified; live operation untested | workspace_reap | core |
| `aq question` | Help verified | handwritten | handwritten/unimplemented |
| `aq question answer` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq question escalate` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq question list` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq reply` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq restart` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq schema` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session` | Help verified | handwritten | handwritten/unimplemented |
| `aq session attach` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session drain-ack` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session kill` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session list` | Live read verified; JSON format varies | handwritten | handwritten/unimplemented |
| `aq session logs` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session nudge` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session peek` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session show` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session sleep` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session token` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq session wake` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq start` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq status` | Live read verified; JSON format varies | handwritten | handwritten/unimplemented |
| `aq stop` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq stream` | Help verified | handwritten | handwritten/unimplemented |
| `aq stream kill` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq stream start` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq stream tail` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq subagent` | Help verified | handwritten | handwritten/unimplemented |
| `aq subagent event` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq system` | Help verified | handwritten | handwritten/unimplemented |
| `aq system advance-workflow-stage` | Mock dispatch verified; live operation untested | advance_workflow_stage | core |
| `aq system claude-usage` | Mock dispatch verified; live operation untested | claude_usage | core |
| `aq system config` | Help verified | handwritten | handwritten/unimplemented |
| `aq system config edit` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq system config get` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq system config schema` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq system config set` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq system create-workflow` | Mock dispatch verified; live operation untested | create_workflow | core |
| `aq system db-preflight-hierarchy` | Mock dispatch verified; live operation untested | db_preflight_hierarchy | core |
| `aq system delivery-promote` | Mock dispatch verified; live operation untested | delivery_promote | core |
| `aq system delivery-receipts` | Mock dispatch verified; live operation untested | delivery_receipts | core |
| `aq system doctor` | Mock dispatch verified; live operation untested | doctor | core |
| `aq system edit-intelligence-class` | Mock dispatch verified; live operation untested | edit_intelligence_class | core |
| `aq system find-applicable-tool` | Mock dispatch verified; live operation untested | find_applicable_tool | core |
| `aq system get-chat-analyzer-metrics` | Mock dispatch verified; live operation untested | get_chat_analyzer_metrics | core |
| `aq system get-config` | Mock dispatch verified; live operation untested | get_config | core |
| `aq system get-config-schema` | Mock dispatch verified; live operation untested | get_config_schema | core |
| `aq system get-costs` | Live read verified; JSON format varies | get_costs | core |
| `aq system get-recent-events` | Mock dispatch verified; live operation untested | get_recent_events | core |
| `aq system get-schema` | Mock dispatch verified; live operation untested | get_schema | core |
| `aq system get-stuck-tasks` | Live read verified; JSON format varies | get_stuck_tasks | core |
| `aq system get-system-channel` | Mock dispatch verified; live operation untested | get_system_channel | core |
| `aq system get-token-usage` | Mock dispatch verified; live operation untested | get_token_usage | core |
| `aq system get-workflow` | Mock dispatch verified; live operation untested | get_workflow | core |
| `aq system integration-build-candidate` | Mock dispatch verified; live operation untested | integration_build_candidate | core |
| `aq system integration-checkpoint-parent` | Mock dispatch verified; live operation untested | integration_checkpoint_parent | core |
| `aq system integration-ci-evidence` | Mock dispatch verified; live operation untested | integration_ci_evidence | core |
| `aq system integration-cleanup` | Mock dispatch verified; live operation untested | integration_cleanup | core |
| `aq system integration-complete-parent` | Mock dispatch verified; live operation untested | integration_complete_parent | core |
| `aq system integration-delivery-readiness` | Mock dispatch verified; live operation untested | integration_delivery_readiness | core |
| `aq system integration-file-children` | Mock dispatch verified; live operation untested | integration_file_children | core |
| `aq system integration-mutate-hierarchy` | Mock dispatch verified; live operation untested | integration_mutate_hierarchy | core |
| `aq system integration-parent-verify` | Mock dispatch verified; live operation untested | integration_parent_verify | core |
| `aq system integration-promote-main` | Mock dispatch verified; live operation untested | integration_promote_main | core |
| `aq system integration-push-conflict-resolution` | Mock dispatch verified; live operation untested | integration_push_conflict_resolution | core |
| `aq system integration-reconcile-promotion` | Mock dispatch verified; live operation untested | integration_reconcile_promotion | core |
| `aq system integration-record-repair` | Mock dispatch verified; live operation untested | integration_record_repair | core |
| `aq system integration-release` | Mock dispatch verified; live operation untested | integration_release | core |
| `aq system integration-repair-dispatch` | Mock dispatch verified; live operation untested | integration_repair_dispatch | core |
| `aq system integration-repair-start` | Mock dispatch verified; live operation untested | integration_repair_start | core |
| `aq system integration-repair-timeout` | Mock dispatch verified; live operation untested | integration_repair_timeout | core |
| `aq system integration-resolve-conflict` | Mock dispatch verified; live operation untested | integration_resolve_conflict | core |
| `aq system integration-schedule-due` | Mock dispatch verified; live operation untested | integration_schedule_due | core |
| `aq system integration-seal` | Mock dispatch verified; live operation untested | integration_seal | core |
| `aq system integration-transfer-owner` | Mock dispatch verified; live operation untested | integration_transfer_owner | core |
| `aq system list-event-triggers` | Live read verified; JSON format varies | list_event_triggers | core |
| `aq system list-intelligence-classes` | Live read verified; JSON format varies | list_intelligence_classes | core |
| `aq system list-prompts` | Mock dispatch verified; live operation untested | list_prompts | core |
| `aq system list-workflows` | Live read verified; JSON format varies | list_workflows | core |
| `aq system migrate-profiles` | Mock dispatch verified; live operation untested | migrate_profiles | core |
| `aq system orchestrator-control` | Mock dispatch verified; live operation untested | orchestrator_control | core |
| `aq system provide-input` | Mock dispatch verified; live operation untested | provide_input | core |
| `aq system provider-usage-probe` | Mock dispatch verified; live operation untested | provider_usage_probe | core |
| `aq system read-logs` | Mock dispatch verified; live operation untested | read_logs | core |
| `aq system read-prompt` | Mock dispatch verified; live operation untested | read_prompt | core |
| `aq system reload-config` | Mock dispatch verified; live operation untested | reload_config | core |
| `aq system render-prompt` | Mock dispatch verified; live operation untested | render_prompt | core |
| `aq system scan-stub-staleness` | Mock dispatch verified; live operation untested | scan_stub_staleness | core |
| `aq system session-input` | Mock dispatch verified; live operation untested | session_input | core |
| `aq system task-route-options` | Mock dispatch verified; live operation untested | task_route_options | core |
| `aq system token-audit` | Mock dispatch verified; live operation untested | token_audit | core |
| `aq system update-config` | Mock dispatch verified; live operation untested | update_config | core |
| `aq system vault-rebuild-index` | Mock dispatch verified; live operation untested | vault_rebuild_index | core |
| `aq system workflow-pipeline-view` | Mock dispatch verified; live operation untested | workflow_pipeline_view | core |
| `aq task` | Help verified | handwritten | handwritten/unimplemented |
| `aq task add-dependency` | Mock dispatch verified; live operation untested | add_dependency | core |
| `aq task archive` | Mock dispatch verified; live operation untested | archive_task | core |
| `aq task archive-settings` | Mock dispatch verified; live operation untested | archive_settings | core |
| `aq task ask-human` | Unimplemented core handler; runtime unverified | ask_human | handwritten/unimplemented |
| `aq task batch-commit` | Mock dispatch verified; live operation untested | task_batch_commit | core |
| `aq task batch-discard` | Mock dispatch verified; live operation untested | task_batch_discard | core |
| `aq task batch-propose` | Mock dispatch verified; live operation untested | task_batch_propose | core |
| `aq task batch-update` | Mock dispatch verified; live operation untested | task_batch_update | core |
| `aq task children` | Mock dispatch verified; live operation untested | task_children | core |
| `aq task claim` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task close` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task comment` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task comment-delete` | Mock dispatch verified; live operation untested | task_comment_delete | core |
| `aq task comment-edit` | Mock dispatch verified; live operation untested | task_comment_edit | core |
| `aq task comments` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task create` | Broken partial flags and JSON; full flags unit-tested | handwritten | handwritten/unimplemented |
| `aq task delete` | Mock dispatch verified; live operation untested | delete_task | core |
| `aq task deps` | Live read verified; JSON format varies | task_deps | core |
| `aq task details` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task edit` | Mock dispatch verified; live operation untested | edit_task | core |
| `aq task ensure` | Mock dispatch verified; live operation untested | ensure_task | core |
| `aq task explain` | Live read verified; JSON format varies | explain_task | core |
| `aq task gate-create` | Mock dispatch verified; live operation untested | gate_create | core |
| `aq task gate-list` | Live read verified; JSON format varies | gate_list | core |
| `aq task gate-resolve` | Mock dispatch verified; live operation untested | gate_resolve | core |
| `aq task gate-show` | Mock dispatch verified; live operation untested | gate_show | core |
| `aq task get` | Live read verified; JSON format varies | get_task | core |
| `aq task get-chain-health` | Mock dispatch verified; live operation untested | get_chain_health | core |
| `aq task get-dependencies` | Mock dispatch verified; live operation untested | get_task_dependencies | core |
| `aq task get-downstream` | Mock dispatch verified; live operation untested | get_downstream_tasks | core |
| `aq task get-result` | Mock dispatch verified; live operation untested | get_task_result | core |
| `aq task get-tree` | Mock dispatch verified; live operation untested | get_task_tree | core |
| `aq task heartbeat` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task list` | Live read verified; JSON format varies | handwritten | handwritten/unimplemented |
| `aq task list-active-all-projects` | Mock dispatch verified; live operation untested | list_active_tasks_all_projects | core |
| `aq task list-archived` | Mock dispatch verified; live operation untested | list_archived | core |
| `aq task pause` | Mock dispatch verified; live operation untested | pause_task | core |
| `aq task progress` | Mock dispatch verified; live operation untested | task_progress | core |
| `aq task recent-activity` | Live read verified; JSON format varies | task_recent_activity | core |
| `aq task recover` | Mock dispatch verified; live operation untested | task_recover | core |
| `aq task remove-dependency` | Mock dispatch verified; live operation untested | remove_dependency | core |
| `aq task reopen-with-feedback` | Mock dispatch verified; live operation untested | reopen_with_feedback | core |
| `aq task reparent` | Mock dispatch verified; live operation untested | reparent_task | core |
| `aq task restart` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task resume` | Mock dispatch verified; live operation untested | resume_task | core |
| `aq task route` | Mock dispatch verified; live operation untested | task_route | core |
| `aq task search` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task select` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task set` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task set-status` | Mock dispatch verified; live operation untested | set_task_status | core |
| `aq task show` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq task skip` | Mock dispatch verified; live operation untested | skip_task | core |
| `aq task spec-approve` | Mock dispatch verified; live operation untested | spec_approve | core |
| `aq task stop` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq test` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq vault` | Help verified | handwritten | handwritten/unimplemented |
| `aq vault migrate` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
| `aq vault reset-harness` | Help verified; see area tests; live operation untested | handwritten | handwritten/unimplemented |
