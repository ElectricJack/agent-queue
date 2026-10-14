# Authoring and rolling out reusable playbook steps

The compiler reads reviewed Markdown and returns exactly `rules` and `steps`.
The server supplies identity, version, source hash, dependency fingerprints,
feature requirements and trusted execution pins. The existing `integration_policy`
compatibility field must equal the source block; it cannot invent policy.

## Discover before compiling

`aq playbook commands` exposes input schemas, result modes, outcomes, effects,
preview/retry policy and caller requirements. `doctor_catalog` describes check
scope and completeness support. Coverage is independent from permission: generated
contracts include review verbs, but no PLAYBOOK principal becomes a supervisor,
human decider or claimed worker. `run_command`, `shutdown`, `restart_daemon` and
`update_and_restart` are denied. Scope/capability refusals remain `unauthorized`;
unknown events remain `unknown_event`.

Generic results are the original dictionary, including strictly boolean `success`.
Read actual keys, not a fabricated `payload` or `root`. Missing/string/integer
success flags, non-JSON values and oversized results are `contract_violation`.
Typed commands retain their declared outcomes, such as `read`.

## Source declarations and semantic alignment

Use backticks for command names, bindings and exact result paths. State executable
policy clauses together: `assigned_threshold_seconds` = `1800`,
`repeat_after_seconds` = `null`, or predicate `seconds_in_state` `gt` `1800`.
For proposals using new collection/observation/state/script features or explicit
`required_capabilities`, limits, recipient fields and comparison clauses without source evidence
produce a blocking question. Review the complete graph too: these checks establish
literal evidence, not a proof of natural-language equivalence or policy intent.
Legacy sources retain their existing identifier checks and golden artifact identities;
they are not automatically rewritten to adopt this stronger authoring contract.

Frontmatter may declare:

```yaml
state_schema_version: 1
required_capabilities: [playbook_state_list, playbook_state_notify, message_send]
scripts:
  report:
    runtime: python
    argv: [bin/python, {type: literal, value: '--version'}]
    supporting_files: []
    limits: {wall_seconds: 60, stdout_bytes: 1048576, stderr_bytes: 1048576}
    output: text
```

`required_capabilities` is a unique list of command names for review, not a grant.
The one exception is a declared dependency of a step's own command: a
`playbook_state_notify` step's artifact grants `message_send` only when the pinned
declaration names it (`DECLARED_COMMAND_DEPENDENCIES` in `src/playbooks/definition.py`).
State versions are strict positive integers (default 1 for state users). Script
manifests are strict `ScriptProgram` objects: exactly one script/argv/shell form,
literal runtime/interpreter/source identity, declared supporting files, typed data
arguments, working-directory identifier, environment allowlist, limits and output
policy. Requested policy lives in each program; unsupported isolation fields fail
validation. The compiled program must equal the source manifest and server-resolved
pin. No source field may provide a principal, namespace owner, resolved digest or
trusted execution context. Changed program or dependency bytes require new pins.

## Collections and complete observations

`filter` and `map` bind items lexically; an inner binding can shadow an outer item
without changing it. `len` takes a list. `diff` preserves stable first-occurrence
left identities absent from right; booleans and numbers are distinct identities.
Missing paths/null operands fail unless explicitly coalesced. Evaluation is capped
at depth 10, 100,000 visits, 10,000 collection items and 262,144 encoded bytes.
Nested foreach depth is at most 4, each loop defaults to 500 iterations (maximum
10,000), and a run permits 100,000 iteration entries across restarts.

Snapshot mode freezes a scan once and returns finite `page_tokens`. Read every
page without rerunning the producer. TTL is 30 minutes, pages contain at most 500
rows and 192 KiB, and whole responses including manifests fit 262,144 bytes.
Storage ceilings are 64 MiB/run, 256 MiB/scope and 2 GiB/host; exhaustion refuses
new snapshots rather than evicting valid ones. Producer `complete` and successful
page/loop traversal are separate gates. Expired/missing pages, failed loops and
partial coverage cannot clear prior findings.

`task_observations_v1` contains `claims.holder_consistency` and `stall.sweep`, both
system-only. Project callers cannot scan globally then filter. Unknown/unported
checks, unsupported scopes, timeouts and unavailable dependencies are explicit
incomplete/failure outcomes. Unhealthy findings can still be successful observations.

## State and script execution

Save notification-state revisions before scans. Clear absent keys only with those
saved revisions; a conflict requires fresh evidence, not rereading a revision for
the old scan. Stable subject/source keys define episodes. Changed wording/age does
not re-alert. Null reminder intervals disable repeats; recurrence after clear starts
a new episode. State, tombstones and dedupe journals survive restart, recompile,
disable/re-enable and removal. Operator reset and reviewed version migration are
explicit and can re-alert; never purge history to make a retry succeed.
See [state](playbook-state.md) for quotas and reset/replay-floor semantics.

Scripts are ordinary host processes, with no cgroup/namespace/network isolation,
launcher or prerequisite doctor gate. `resources.scripts.enabled` defaults true;
job admission must also be enabled. Configure runtime manifests, immutable input
storage, state/output storage and protected-value encryption keys. Configure
`playbooks.private_value_active_key_id` and `playbooks.private_value_key_env` with
environment variable references; never put key bytes in source or artifacts. The existing
pins verify declared bytes, not undeclared host dependencies. Keep dynamic data in
argv/stdin/explicit environment values, never substituted into program text.
Timeout/output caps and complete-output JSON parsing remain enforced.

Ambiguous launch/completion pauses for operator decision; it never authorizes an
automatic second execution. Dry-run/shadow launches no process and performs no
state/message/job writes. Public receipts show identities and protected handles.
Authorized `job_logs` reads bounded stdout/stderr ranges after checking run access;
knowing a handle grants nothing. Missing keys refuse protected binding/readback,
never plaintext fallback. See [script configuration](playbook-script-sandbox.md).

## Deliberate rollout and rollback

1. Validate this checkout's focused compiler, graph, activation and dashboard tests.
   Keep stored legacy golden hashes; empty additive metadata must not change them.
   Tests on this box suffice for scripts; there is no separate real-host gate.
2. The operator installs protected storage/read keys and upgrades additive database
   schema and all intended executors through normal deployment. Workers never
   migrate the operator database or manage its daemon. Unsupported feature pins
   make only affected activations unready.
3. Enumerate artifacts affected by intentional command/schema/source changes.
   Rebuild only those pins, inspect semantic diffs, update bundle manifest digests
   and copy reviewed fixtures to `src/prompts/reviewed_playbooks/<id>/`. Regenerate
   schemas and SDKs from source; never hand-edit clients or globally refresh an
   installed playbook on startup. A recorded bundle is not acceptance evidence.
4. Shipped profiles and skills are write-if-absent. Operator drift checks are
   `aq doctor --check profiles.system_drift` and `aq doctor --check skills.installed_drift`.
   Reconcile installed prose deliberately; `aq agent profile-reseed --profile-id
   playbook-compiler --grants-only` merges the minimum catalogue grants while
   preserving prose/operator edits. It does not update the compiler's guidance.
5. Review exact source/artifact and explicitly import/activate. The optional watch
   is not startup-required and is never installed by this authoring change.
6. Before rollback disable affected new-feature activations and script admission;
   settle or explicitly cancel running jobs, pausing unresolved runs. Restore
   compatible reviewed artifacts deliberately. Keep additive tables/columns,
   encrypted values, snapshots, state and journals. Never reset versions/drop state
   to make old code load. Retain old read keys until referenced data is retired.
