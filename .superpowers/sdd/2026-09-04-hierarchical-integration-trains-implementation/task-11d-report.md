# Task 11d report — operational CLI, doctor, and guide

Status: DONE_WITH_CONCERNS

Task baseline: `7a13b6f3836d028e7824e6192ee13318ae65b33f`.
Reviewed Task 11c backend: `534666d5`. The controller supplied the narrow
per-project cadence amendment in `e52f9628` while this slice was active.
`operational-scope-override.md` remains binding: no security certification,
scratch probe, broad recovery matrix, production mutation, deployment, or backend
copier work was performed.

## Delivered checklist

- [x] Added a hand-crafted `aq integration` group before auto-registration with
  `status`, `flush`, `enable`, `waive-history`, `resume`, `abort`, and
  `retry-cleanup`. Every command uses the existing generic execute client and
  shared JSON/brief/error envelope; no CLI implementation opens the database or
  credential files.
- [x] Kept the deferred `probe` command absent from hand-crafted and discovered
  CLI surfaces.
- [x] Preserved structured stale/blocked details. Brief status retains the
  generation, effective/desired modes, draining state, blockers, and exact blocker
  digest needed for a subsequent guarded operator decision.
- [x] Added guarded `aq project set` transport for
  `integration-repository-id` and `integration-policy`, requiring an explicit
  nonnegative expected integration generation. The operator reason is optional;
  when omitted, the backend records `configure hierarchical integration`. Policy
  input must parse as a JSON object. Rollout mode remains exclusive to `aq
  integration enable`.
- [x] Added report-only `integration.operational` doctor output over the reviewed
  status service: modes, generation, drain/readiness, repository, blockers/digest,
  deferred certification, active batch, human-required work, and cleanup
  attention. It has no fix callback and does not probe, migrate, edit config, or
  mutate provider state. The existing unreviewed-PR check remains registered.
- [x] Added optional positive `interval_seconds` to the existing typed
  `integration_enable` contract and LOCAL-only handler. It is accepted only with
  `mode=train` and is applied inside the same hierarchy-locked generation-CAS
  transaction; the public scheduler configure method is not nested.
- [x] A new train schedule uses the supplied interval or the 300-second default.
  Omitting it preserves an existing interval/next-due value; changing it computes
  `next_due_at = now + interval_seconds` deterministically while preserving request
  sequence, outstanding/manual request, and catch-up state. Same-mode train updates
  still advance generation and append the normal reasoned transition audit.
- [x] Non-train, zero, and negative intervals fail before writes. A supplied
  cadence during drain returns a structured blocker without changing generation,
  schedule, or transition history. Stale cadence updates are also zero-write.
- [x] Corrected the shipped MCP/CLI/API discovery path to project typed command
  contract presentation and `args_model` JSON schema before legacy fallbacks.
  Inventory tests now inspect the effective static-plus-contract definitions;
  contract-backed `gate_create`, `gate_resolve`, and `task_route_options` fallback
  debt was removed rather than whitelisting the new integration commands or
  relaxing uniqueness/orphan/schema ratchets. A regression verifies the real
  `integration_enable` interval/generation schema and the absence of
  `integration_probe`.
- [x] Added an operator guide with actual config and policy shapes, exact command
  spellings, current-backend schema upgrade, GitHub App `Variables: read`, reviewed
  artifact/profile/class bindings, observe/hierarchy/train sequence, configurable
  cadence, human controls, cleanup retry, and disable/drain rollback.
- [x] The guide clearly distinguishes deployment-isolation responsibility from a
  nonexistent certification gate. It does not prescribe
  `api_auth.require_session_token: true` without a separately reviewed LOCAL
  operator path, because session principals cannot invoke LOCAL-only controls.
- [x] The guide states that populated SQLite-to-PostgreSQL copying is unsupported:
  both existing copiers omit integration state. Same-backend in-place schema
  upgrade is the supported release path.

## Shipped command surface

All arguments below are sent through generic `execute` and validated again by the
daemon contract/control boundary.

- `aq integration status PROJECT_ID`
- `aq integration flush PROJECT_ID`
- `aq integration enable PROJECT_ID --mode disabled|observe|hierarchy|train
  --expected-generation GENERATION --reason REASON [--waiver-id WAIVER_ID]
  [--interval-seconds SECONDS]`; the interval option is positive and train-only.
- `aq integration waive-history PROJECT_ID --reason REASON --blocker-digest DIGEST`
- `aq integration resume OPERATION_ID`
- `aq integration abort OPERATION_ID --reason REASON`
- `aq integration retry-cleanup BATCH_ID`
- `aq project set PROJECT_ID integration-repository-id VALUE
  --expected-integration-generation GENERATION [--reason REASON]`
- `aq project set PROJECT_ID integration-policy POLICY_JSON
  --expected-integration-generation GENERATION [--reason REASON]`

## RED evidence

- `pytest -q tests/test_cli_integration.py -x` -> `1 failed, 3 warnings in
  1.65s`; `src.cli.integration` did not exist.
- `pytest -q tests/test_cli_projects.py::test_project_set_forwards_guarded_integration_configuration -x`
  -> `1 failed, 3 warnings in 1.65s`; the guarded generation option was absent.
- `pytest -q tests/test_doctor_integration_checks.py::test_operational_check_reports_modes_blockers_and_human_attention_read_only -x`
  -> `1 failed, 3 warnings in 1.01s`; `integration.operational` was unregistered.
- The first combined affected-area gate,
  `aq test tests/test_cli_integration.py tests/test_cli_projects.py tests/test_cli_envelope.py tests/test_doctor_integration_checks.py tests/test_command_surface.py tests/test_cli_module_entry.py tests/test_migration_guard.py::TestRunSchemaSetup::test_worker_refuses_to_create_the_production_schema`,
  returned `145 passed, 2 failed, 11 warnings in 20.12s`. Both failures were real
  command-discovery inventory gaps: the public transport path ignored typed
  contract schemas, leaving 31 contract-backed commands unplaced and 30
  integration/delivery handlers with empty transport schemas. This was corrected
  in production discovery, not hidden in a whitelist.
- Cadence RED:
  `pytest -q tests/test_cli_integration.py tests/test_integration_operational_controls.py -k 'interval or cadence' -x`
  -> `1 failed, 3 passed, 28 deselected, 3 warnings in 1.88s`; the control service
  rejected the new keyword because `interval_seconds` was not implemented.

## GREEN and verification evidence

Focused pre-amendment milestones:

- `pytest -q tests/test_cli_integration.py -x` -> `12 passed, 3 warnings in
  1.45s`.
- `pytest -q tests/test_doctor_integration_checks.py -x` -> `10 passed,
  3 warnings in 1.59s`.
- Guarded project-set nodes -> `2 passed, 3 warnings in 1.50s`.
- `pytest -q tests/test_cli_envelope.py::TestBriefProjections -x` -> `9 passed,
  3 warnings in 0.81s`.

Cadence/discovery milestones:

- `pytest -q tests/test_cli_integration.py tests/test_integration_operational_controls.py -k 'interval or cadence' -x`
  -> `5 passed, 28 deselected, 3 warnings in 1.91s`.
- `pytest -q tests/test_integration_operational_controls.py::test_public_control_authority_keeps_enable_local_and_status_project_scoped -x`
  -> `1 passed, 3 warnings in 1.20s`.
- `pytest -q tests/test_command_surface.py -x` -> `56 passed, 3 warnings in
  2.14s` after production contract projection and debt removal.
- Final amended-file gate:
  `aq test tests/test_cli_integration.py tests/test_integration_operational_controls.py tests/test_integration_contracts.py tests/test_command_surface.py`
  -> `100 passed, 11 warnings in 6.55s`.

Static/document checks:

- `python3` read-only validation of the guide's fenced YAML through
  `GitHubAppConfig`/`IntegrationConfig.validate()` and its fenced JSON through
  `HierarchicalIntegrationPolicy.model_validate()` ->
  `guide config and policy examples validated`. An initial identical invocation
  with unavailable `python` returned shell exit 127; `python3` is the available
  repository interpreter.
- `ruff check src/cli/app.py src/cli/auto_commands.py src/cli/envelope.py src/cli/integration.py src/cli/projects.py src/commands/contracts/integration.py src/commands/integration_commands.py src/doctor/integration_checks.py src/integration/controls.py src/mcp_registration.py src/tools/definitions.py tests/test_cli_envelope.py tests/test_cli_integration.py tests/test_cli_projects.py tests/test_command_surface.py tests/test_doctor_integration_checks.py tests/test_integration_operational_controls.py`
  -> `All checks passed!`.
- `python3 -m compileall -q src/cli/app.py src/cli/auto_commands.py src/cli/envelope.py src/cli/integration.py src/cli/projects.py src/commands/contracts/integration.py src/commands/integration_commands.py src/doctor/integration_checks.py src/integration/controls.py src/mcp_registration.py src/tools/definitions.py`
  -> exit 0.
- `git diff --check` -> exit 0.

The test warnings are inherited `pkg_resources`/namespace and Python `audioop`
deprecations. Per controller direction, the already-executed 145-test area gate was
not repeated after the narrow cadence/discovery amendment; the final 100-test
amended-file gate covers those additions.

## Self-review and limitations

- CLI code contains presentation and transport only; daemon generation-CAS, LOCAL
  authority, exact repository identity, typed policy, blocker digest, and
  irreversible-write protections remain authoritative.
- No API DTO/router or generated client was added: generic execute and the corrected
  existing discovery projection carry the typed schema.
- Security/protection inspection, scratch probes, worker/control-plane isolation
  certification, and broad recovery/PostgreSQL race verification remain explicitly
  deferred. Status and doctor expose certification as `not_performed`; this report
  does not claim deployment readiness.
- The LOCAL operator endpoint must be isolated by the deployment while preserving a
  supported LOCAL operator route. The repository currently has no global
  operator-token flow that can replace it.
- GitHub.com is the only supported forge. The App owner must grant repository
  `Variables: read`; this CLI does not change permissions.
- Populated SQLite-to-PostgreSQL copying remains unsupported and was not modified.
  No production enablement, database/config mutation, probe, deployment, push, PR,
  or main merge was performed.

## Scoped review fix 1

The Important finding in `task-11d-review.md` was verified and corrected. The
optional cadence field now uses strict positive-integer validation in the public
typed contract. The raw command handler no longer calls `int()` on that field, so
direct handler callers pass their original value to the service's independent
strict boundary. Boolean and numeric-string values are rejected by both applicable
boundaries; zero and negative rejection remains covered. No other runtime behavior
changed.

The report's project-configuration wording was also corrected: expected generation
is mandatory, but `--reason` is optional and the backend records `configure
hierarchical integration` when it is omitted.

Exact RED:

- `pytest -q tests/test_integration_contracts.py::test_enable_contract_rejects_coercible_non_integer_intervals tests/test_integration_operational_controls.py::test_raw_enable_handler_does_not_coerce_interval tests/test_integration_operational_controls.py::test_cadence_rejections_and_stale_or_draining_requests_write_nothing -x`
  -> `1 failed, 3 warnings in 1.03s`; `IntegrationEnableArgs` accepted `True`
  without raising `ValidationError`.

Exact GREEN and checks:

- `pytest -q tests/test_integration_contracts.py::test_enable_contract_rejects_coercible_non_integer_intervals tests/test_integration_operational_controls.py::test_raw_enable_handler_does_not_coerce_interval tests/test_integration_operational_controls.py::test_cadence_rejections_and_stale_or_draining_requests_write_nothing -x`
  -> `5 passed, 3 warnings in 1.25s`.
- `ruff check src/commands/contracts/integration.py src/commands/integration_commands.py tests/test_integration_contracts.py tests/test_integration_operational_controls.py && git diff --check`
  -> `All checks passed!`, exit 0.
