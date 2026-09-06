# Task 11 current interface map after WSL recovery

Read-only preparation by /root/controls_interface_map_sol; no implementation,
tests, operator state, or Task10c review. Line numbers are recovery-time hints.

## Config

- `src/config.py`: GitHubAppConfig1469, validate_github_app_raw_config1497,
  load_config2813, ConfigWatcher.reload2449. Extend closed raw validation for
  scratch_probe before substitution and without echoing values.
- Integration is currently hot-reloadable2287; credential caches require the
  Task11 restart-required ruling.
- `src/config_editor.py`: recursive additionalProperties:false219 and schema
  reload metadata257. `src/commands/system_commands.py`: safe get_config197,
  full temporary-document validation update_config253.
- Concrete leak risk: `src/commands/handler.py`918 logs nested update_config.data
  through _preview652. Safe _summarize_args220 does not cover that log. Ensure
  rejected inline credential payloads never reach logs, not merely serialization.

## Authority

- `src/api/auth.py` RequestScope28; `src/api/scope.py` check_command_scope88.
- `src/api/execute.py`60 and codegen151 strip server-owned args and inject scope.
- `src/commands/handler.py`738/876 derive/enforce ExecutionPrincipal;
  `src/commands/principal.py`146 identifies trusted local callers.
- Existing integration relationship checks at integration_commands.py33/240.
- Add strictly local-operator controls, not broad elevated-supervisor authority;
  status/flush remain project-scoped. Resolve operation/batch project server-side.

## Transport

- `src/git/askpass_broker.py`: PinnedFile/GitCredentialTopology/pin helpers20-95,
  runtime /proc/argv/inode/device/owner/digest checks124-207.
- GitManager._app_git_credential_topology3270 currently discovers exec-path;
  credentialed fetch/push obtains runtime pins3040/3371.
- Extend one public inspector here, shared by enablement/runtime: include missing
  file/directory/mode/digest/symlink facts, facilities and explicit worker-write
  authority. Freeze accepted fingerprint and compare at mutation. Worker posture
  facts come from server preflight, not independent Git rediscovery.

## Provider and probe

- GitHubAppClient.bind_repository157 verifies identities/permissions; refresh338.
  exact_head_ref244, authenticated request_json382/paged_items available.
- GitManager exact expected-old push/delete2892.
- IntegrationAttestationService.enablement_blockers237 has injectable protection/
  probe readers but bool/tuple results cannot prove typed bound probe facts.
- Add narrow reads to github_app.py, canonical facts/digest in
  integration/protection.py, durable replay state in integration/probe.py.
  Missing: classic/rulesets facts, hosted workflow variables, distinct capable
  negative principal, bound positive/negative durable receipt.

## Legacy cutover

- _cmd_pr_merge resolves project git_commands.py62, before forge85/push109:
  authoritative hierarchy/train guard belongs here.
- default-pipeline.md125-154 creates final-reviewer/pr-merged routes;
  final-reviewer/profile.md91 calls pr_merge; scope grants312/613.
- project pr-merge-sweep.md1-34; activation query helpers
  playbook_artifact_queries.py258-341, runtime filtering87-115/243-288.
- set_playbook_enabled1565 owns its own transaction and is unsuitable for atomic
  cutover. Use conn-owned integration_control_queries.py helpers.
- Runtime needs per-project suppression of selected system rules. Historical
  gates have no applicability field; preserve rows and record waiver applicability
  separately, as Task11 requires.
