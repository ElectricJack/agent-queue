# Operations module catalog

This catalog maps every production module owned by the operations documentation shard to the [operator guide](../../guides/operations.md). It is intentionally terse; use the guide for procedures and the source links for implementation detail.

| Module | Purpose | Component | Notes |
|---|---|---|---|
| [src/doctor/__init__.py](../../../src/doctor/__init__.py) | Builds the complete default doctor registry. | [Operations guide](../../guides/operations.md) | Covered by `tests/test_doctor.py`. |
| [src/doctor/builtin.py](../../../src/doctor/builtin.py) | Defines generic configuration, database, vault, harness, log, event, MCP, pause and task checks. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_doctor.py`. |
| [src/doctor/capability_checks.py](../../../src/doctor/capability_checks.py) | Reports capability enforcement and legacy/wildcard profile drift. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_capability_operator_surfaces.py`. |
| [src/doctor/db_checks.py](../../../src/doctor/db_checks.py) | Reports database-specific integrity and migration conditions. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_doctor_db_checks.py`. |
| [src/doctor/formula_checks.py](../../../src/doctor/formula_checks.py) | Validates formula source parsing for the doctor catalog. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_formula_doctor.py`. |
| [src/doctor/hierarchy_checks.py](../../../src/doctor/hierarchy_checks.py) | Detects and safely repairs selected task-hierarchy consistency drift. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_hierarchy_doctor.py`. |
| [src/doctor/integration_checks.py](../../../src/doctor/integration_checks.py) | Reports integration operational, branch-discard and ownership-fence recovery conditions. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_doctor_integration_checks.py`. |
| [src/doctor/intelligence_class_checks.py](../../../src/doctor/intelligence_class_checks.py) | Validates intelligence-class markdown definitions. | [Operations guide](../../guides/operations.md) | Focused coverage: intelligence-class doctor tests. |
| [src/doctor/models.py](../../../src/doctor/models.py) | Defines doctor severities, check results, descriptors and execution context. | [Operations guide](../../guides/operations.md) | Covered by `tests/test_doctor.py`. |
| [src/doctor/playbook_v2_checks.py](../../../src/doctor/playbook_v2_checks.py) | Checks Playbooks V2 artifact, activation and replay-policy health. | [Operations guide](../../guides/operations.md) | Focused coverage: Playbooks V2 doctor tests. |
| [src/doctor/pool_checks.py](../../../src/doctor/pool_checks.py) | Diagnoses and narrowly repairs pool claim, launch, agent, checkout and branch conditions. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_pool_doctor.py`. |
| [src/doctor/profile_checks.py](../../../src/doctor/profile_checks.py) | Checks global profile consistency. | [Operations guide](../../guides/operations.md) | Focused coverage: profile doctor tests. |
| [src/doctor/project_checks.py](../../../src/doctor/project_checks.py) | Checks project-root operational configuration. | [Operations guide](../../guides/operations.md) | Focused coverage: project doctor tests. |
| [src/doctor/provider_checks.py](../../../src/doctor/provider_checks.py) | Checks configured provider availability and related diagnostics. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_provider_doctor.py`. |
| [src/doctor/resource_checks.py](../../../src/doctor/resource_checks.py) | Reports cgroup, machine-load and test-pressure resource health. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_resource_doctor.py`. |
| [src/doctor/runner.py](../../../src/doctor/runner.py) | Registers, times out, runs, fixes and summarizes independent doctor checks. | [Operations guide](../../guides/operations.md) | Covered by `tests/test_doctor.py`. |
| [src/doctor/session_checks.py](../../../src/doctor/session_checks.py) | Detects inherited session markers and safely resubmits daemon-marked stuck composers. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_session_doctor.py`. |
| [src/doctor/skill_checks.py](../../../src/doctor/skill_checks.py) | Detects and can refresh installed skill-file drift. | [Operations guide](../../guides/operations.md) | Focused coverage: skill doctor tests. |
| [src/doctor/task_checks.py](../../../src/doctor/task_checks.py) | Detects and clears stale task attention flags. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_task_doctor.py`. |
| [src/doctor/workspace_checks.py](../../../src/doctor/workspace_checks.py) | Checks workspace base-session consistency. | [Operations guide](../../guides/operations.md) | Focused coverage: workspace doctor tests. |
| [src/doctor/worktree_checks.py](../../../src/doctor/worktree_checks.py) | Detects slot worktrees still pinned to deleted-task branches. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_worktree_doctor.py`. |
| [src/logging_config.py](../../../src/logging_config.py) | Configures structured console/JSONL logging and correlation context. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_logging_config.py`. |
| [src/metrics/__init__.py](../../../src/metrics/__init__.py) | Exposes the fleet-metrics package surface. | [Operations guide](../../guides/operations.md) | Covered with sampler tests. |
| [src/metrics/sampler.py](../../../src/metrics/sampler.py) | Collects, publishes, buffers, rolls up and prunes fleet metric samples. | [Operations guide](../../guides/operations.md) | Focused coverage: `tests/test_metrics_sampler.py`. |

## Related pages

* [Operations guide](../../guides/operations.md) — symptom-led diagnosis and recovery procedures.
* [Module catalog index](README.md) — the catalog's shard index and coverage contract.
