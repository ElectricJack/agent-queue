# Operations module catalog

This catalog maps every production module owned by the operations documentation shard to the [operator guide](../../guides/operations.md). It is intentionally terse; use the guide for procedures and the source links for implementation detail.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/benchmark/__init__.py](../../../src/benchmark/__init__.py) | Exposes the benchmark evidence package. | [Benchmark export guide](../../guides/benchmark-routing-export.md) | Read-only reporting surface. |
| [src/benchmark/opencode.py](../../../src/benchmark/opencode.py) | Extracts OpenCode provider and model evidence from explicit session exports. | [Benchmark export guide](../../guides/benchmark-routing-export.md) | Focused coverage: `tests/test_costs.py`. |
| [src/benchmark/report.py](../../../src/benchmark/report.py) | Builds frozen paired reports with route, attempt, stage and cost coverage. | [Benchmark export guide](../../guides/benchmark-routing-export.md) | Focused coverage: `tests/test_costs.py`. |
| [src/doctor/__init__.py](../../../src/doctor/__init__.py) | Builds the complete default doctor registry. | [Operations guide](../../guides/operations.md) | Covered by `tests/test_doctor.py`. |
| [src/doctor/ask_checks.py](../../../src/doctor/ask_checks.py) | Reports human-ask lifecycle and migration health. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/builtin.py](../../../src/doctor/builtin.py) | Defines generic configuration, database, vault, harness, log, event, MCP, pause and task checks. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_doctor.py`. |
| [src/doctor/capability_checks.py](../../../src/doctor/capability_checks.py) | Reports capability enforcement and legacy/wildcard profile drift. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_capability_operator_surfaces.py`. |
| [src/doctor/dashboard_server_checks.py](../../../src/doctor/dashboard_server_checks.py) | Checks and repairs dashboard server availability. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/dashboard_state_checks.py](../../../src/doctor/dashboard_state_checks.py) | Finds orphaned dashboard state. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/db_checks.py](../../../src/doctor/db_checks.py) | Reports database-specific integrity and migration conditions. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_doctor_db_checks.py`. |
| [src/doctor/discord_config_checks.py](../../../src/doctor/discord_config_checks.py) | Checks Discord configuration and channel health. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/escalation_checks.py](../../../src/doctor/escalation_checks.py) | Reports escalation consistency. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/formula_checks.py](../../../src/doctor/formula_checks.py) | Validates formula source parsing for the doctor catalog. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_formula_doctor.py`. |
| [src/doctor/git_checks.py](../../../src/doctor/git_checks.py) | Checks repository state and stale branches. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/hierarchy_checks.py](../../../src/doctor/hierarchy_checks.py) | Detects and safely repairs selected task-hierarchy consistency drift. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_hierarchy_doctor.py`. |
| [src/doctor/integration_checks.py](../../../src/doctor/integration_checks.py) | Reports delivery engine, train, promotion, branch-origin and owner recovery conditions. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_doctor_integration_checks.py`. |
| [src/doctor/intelligence_class_checks.py](../../../src/doctor/intelligence_class_checks.py) | Validates intelligence-class markdown definitions. | [Operations guide](../../guides/operations.md) | Focused coverage: intelligence-class doctor tests. |
| [src/doctor/models.py](../../../src/doctor/models.py) | Defines doctor severities, check results, descriptors and execution context. | [Operations guide](../../guides/operations.md) | Covered by `tests/test_doctor.py`. |
| [src/doctor/observations.py](../../../src/doctor/observations.py) | Distinguishes unavailable observations from observed absence. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/perf_checks.py](../../../src/doctor/perf_checks.py) | Reports loop lag and health latency. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/playbook_v2_checks.py](../../../src/doctor/playbook_v2_checks.py) | Checks Playbooks V2 artifact, activation and replay-policy health. | [Operations guide](../../guides/operations.md) | Focused coverage: Playbooks V2 doctor tests. |
| [src/doctor/pool_checks.py](../../../src/doctor/pool_checks.py) | Diagnoses and narrowly repairs pool claim, launch, agent, checkout and branch conditions. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_pool_doctor.py`. |
| [src/doctor/profile_checks.py](../../../src/doctor/profile_checks.py) | Checks global profile consistency. | [Operations guide](../../guides/operations.md) | Focused coverage: profile doctor tests. |
| [src/doctor/project_checks.py](../../../src/doctor/project_checks.py) | Checks project-root operational configuration. | [Operations guide](../../guides/operations.md) | Focused coverage: project doctor tests. |
| [src/doctor/provider_availability_checks.py](../../../src/doctor/provider_availability_checks.py) | Reports provider availability, a stalled recovery loop, an inactive `provider-failover` playbook, and work held too long by an unavailable provider. | [Provider outage runbook](../../guides/provider-outage.md) | Report-only by design; invoked through daemon-backed doctor. Focused coverage: `tests/test_provider_availability_doctor.py`. |
| [src/doctor/provider_checks.py](../../../src/doctor/provider_checks.py) | Checks configured provider availability and related diagnostics. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_provider_doctor.py`. |
| [src/doctor/record_checks.py](../../../src/doctor/record_checks.py) | Checks durable record consistency. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/resource_checks.py](../../../src/doctor/resource_checks.py) | Reports cgroup, machine-load and test-pressure resource health. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_resource_doctor.py`. |
| [src/doctor/review_checks.py](../../../src/doctor/review_checks.py) | Reports review workflow health. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/routing_checks.py](../../../src/doctor/routing_checks.py) | Reports routing configuration health. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/runner.py](../../../src/doctor/runner.py) | Registers, times out, runs, fixes and summarizes independent doctor checks. | [Operations guide](../../guides/operations.md) | Covered by `tests/test_doctor.py`. |
| [src/doctor/service_checks.py](../../../src/doctor/service_checks.py) | Reports installed watchdog service health. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/session_checks.py](../../../src/doctor/session_checks.py) | Detects inherited session markers and safely resubmits daemon-marked stuck composers. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_session_doctor.py`. |
| [src/doctor/skill_checks.py](../../../src/doctor/skill_checks.py) | Detects and can refresh installed skill-file drift. | [Operations guide](../../guides/operations.md) | Focused coverage: skill doctor tests. |
| [src/doctor/stall_checks.py](../../../src/doctor/stall_checks.py) | Runs the read-only operator stall sweep. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/task_checks.py](../../../src/doctor/task_checks.py) | Detects and clears stale task attention flags. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_task_doctor.py`. |
| [src/doctor/wait_checks.py](../../../src/doctor/wait_checks.py) | Inventories verified-wait schema and repairs orphan waits through commands. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/doctor/workspace_checks.py](../../../src/doctor/workspace_checks.py) | Checks workspace base-session consistency. | [Operations guide](../../guides/operations.md) | Focused coverage: workspace doctor tests. |
| [src/doctor/worktree_checks.py](../../../src/doctor/worktree_checks.py) | Detects slot worktrees still pinned to deleted-task branches. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_worktree_doctor.py`. |
| [src/logging_config.py](../../../src/logging_config.py) | Configures structured console/JSONL logging and correlation context. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_logging_config.py`. |
| [src/metrics/__init__.py](../../../src/metrics/__init__.py) | Exposes the fleet-metrics package surface. | [Operations guide](../../guides/operations.md) | Covered with sampler tests. |
| [src/metrics/delivery_latency.py](../../../src/metrics/delivery_latency.py) | Reports generation-based approval and completion latency from fresh delivery observations. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/metrics/histogram.py](../../../src/metrics/histogram.py) | Aggregates bounded histograms for performance telemetry. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/metrics/host.py](../../../src/metrics/host.py) | Samples host pressure within a time budget. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/metrics/loop_watchdog.py](../../../src/metrics/loop_watchdog.py) | Logs overdue event-loop heartbeats from a separate thread. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/metrics/perf.py](../../../src/metrics/perf.py) | Collects performance counters and duration histograms. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/metrics/relay.py](../../../src/metrics/relay.py) | Samples dashboard relay metrics. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/metrics/sampler.py](../../../src/metrics/sampler.py) | Collects, publishes, buffers, rolls up and prunes fleet metric samples. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_metrics_sampler.py`. |
| [src/metrics/token_efficiency.py](../../../src/metrics/token_efficiency.py) | Builds read-only token-efficiency reports. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/metrics/waits.py](../../../src/metrics/waits.py) | Reports wait populations, released slot time and resume estimates. | [Operations guide](../../guides/operations.md) | Current source surface. |
| [src/stall_watch.py](../../../src/stall_watch.py) | Detects orphaned workers, aged active tasks and unroutable READY leaves, and formats supervisor summaries. | [Stuck-task watch spec](../../specs/stuck-task-watch.md) | Read-only; covered by `tests/test_stall_watch.py`. |

## Related pages

* [Operations guide](../../guides/operations.md) — symptom-led diagnosis and recovery procedures.
* [Module catalog index](README.md) — the catalog's shard index and coverage contract.
