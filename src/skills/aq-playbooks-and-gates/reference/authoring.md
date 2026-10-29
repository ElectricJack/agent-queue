# Playbook authoring, compilation and runtime contracts

Reference for the aq-playbooks-and-gates skill ([../SKILL.md](../SKILL.md)):
what an author or reviewer of a Playbook V2 policy needs beyond running and
resolving playbooks. Submitting a playbook for approval is in the aq-reviews
skill.

## Contents

- Compilation (compiler-as-agent path)
- Catalogue and command results
- Snapshots and page tokens
- State
- Scripts
- Frontmatter declarations

## Compilation (compiler-as-agent path)

Markdown saves do not automatically enqueue compiler tasks. Explicitly arrange an
author/compiler-agent task for prose source, prepare a semantic body containing
only `rules` and `steps`, and iterate with `aq playbook v2-propose`. Proposal is
read-only and does not activate. Review the exact source/artifact bundle, import
it, then deliberately activate its hash through the authorized operator path.

`aq playbook update-source` writes full Markdown with optional source-hash CAS.
For `kind: pipeline`, it lowers synchronously and directly updates the existing
activation on success, preserving the enabled bit; it bypasses the ordinary
activation-diff path. For prose it leaves source saved and reports `compiled: false`;
a proposal is still needed. Failure leaves the previous active artifact live.

A compile failure needs an explicit revision: inspect the proposal diagnostics,
correct the source or semantic body, and re-propose. A file save does not
schedule a fresh compile, and compiler agents never activate policy themselves.

## Catalogue and command results

Discover commands with `aq playbook commands` (`playbook_commands`) and diagnostic
scope/coverage with `doctor_catalog`. Generated coverage includes review verbs;
it does not grant authority, human decisions, session ownership or cross-project
access. `run_command`, `shutdown`, `restart_daemon`, `update_and_restart` are denied.
Generic results preserve their raw object paths and require a boolean `success`;
malformed results are `contract_violation`, denied callers `unauthorized`.

## Snapshots and page tokens

Use finite snapshot `page_tokens` (30-minute TTL, up to 500 rows/192 KiB per page,
262,144-byte response). Producer completeness and successful traversal are separate
requirements. `task_observations_v1` starts system-only; a project cannot scan the
whole system then filter. Unknown/unported checks and unsupported scope are explicit
failures, not healthy empty scans. Never clear on partial/expired coverage.

## State

State survives recompilation, restart, disable/re-enable and source deletion.
CAS clears use revisions saved before scans, never refreshed revisions with old
absence evidence. Notification episodes suppress unchanged findings; null reminders
mean no repeat. Explicit operator reset/version migration can re-alert and retains
replay history. See `docs/guides/playbook-state.md` for quotas and reset semantics.

## Scripts

Scripts run as ordinary processes; `resources.scripts.enabled` defaults true.
Jobs admission, runtime/input configuration and protected-output keys still apply;
no cgroup/namespace/network isolation, launcher or host-prerequisite gate exists.
Timeout and output caps bound execution; ambiguous launch requires operator action,
never automatic replay. Dry-run/shadow launches nothing and leaves values symbolic.
Public receipts contain handles; `job_logs` checks run ownership before returning
bounded protected stdout/stderr. See `docs/guides/playbook-script-runner.md`.

## Frontmatter declarations

Source frontmatter declares `state_schema_version`, `required_capabilities`, and
`scripts` program/dependency/limit policy. These are not compiler-supplied identity
or grants. Review real predicates, program text, settings and protected/unresolved
values. Follow `docs/guides/playbook-building-blocks.md` for deliberate pin rebuilds,
installation drift/reseed and rollback; never refresh installed policy implicitly.
