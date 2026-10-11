# Portable policy profiles (v1)

Policy profiles copy non-personal project policy once without continuing
inheritance. The local operator exports a
project through `policy_export`, previews exact files, and acknowledges the
preview checksum before writing a directory or `.aqpolicy` zip.

Seven typed adapters participate: reviewed playbook source and compiled
artifact; agent profile settings and project overrides; promotion flow; CI check
and test-selection policy; routing preferences; spec, plan and formula templates;
and versioned integration settings. Adapters read fixed policy locations and explicit fields. They never
walk arbitrary vault content, serialize operator config, copy memory/history,
account bindings, secrets, GitHub credentials, provider availability or pool sizes.
Enabled system playbooks and referenced profile templates accompany project policy with
system-origin labels. Required playbook dependencies must have reviewed artifacts.
Derived worker rungs are regenerated locally rather than exported or overwritten.

Legacy compiled artifacts may lack retained Markdown. Startup backfills missing
source files once per artifact using only digest-matching sources from the fixed
vault playbook directories and reviewed bundle catalogues, including inactive
artifacts. Existing retained sources are never replaced. Export uses retained
source first, falling back to a digest-matching vault or catalogue source without
writing during preview. When no exact source is available, export omits that
playbook and any playbooks requiring it, and displays per-item warnings in the CLI
and dashboard preview with instructions to recompile and reactivate the affected
id. Missing compiled artifacts produce a contextual export error, never a raw
filesystem exception. The read-only `playbooks.source_unavailable` doctor check
reports enabled activations whose retained source is missing or fails verification.

The manifest has format/version, name, AQ version, typed item identifiers,
original scope, payload checksums and declared placeholders. Project id,
repository, branch and workspace values become placeholders with no source-value
defaults. Readers validate all types/checksums and bound archive reads without
extracting arbitrary paths. Imports fill placeholders from the target project or
operator values and reject missing values before any write.

`policy_diff` groups new, identical and overwrite items. Every item can go to the
project, global scope or be skipped. Project scope is the default; system-origin
items require an explicit choice. Overwrites are deselected until opted in.
`policy_apply` checks destination fingerprints from that preview before writing.
CLI export/diff/apply and `project create --from-policy` share this selection flow;
the dashboard offers the same preview, per-item scope, overwrite diff and values.

Playbooks are staged and submitted through normal document review, recompiling
against the target registry. They are never activated by import or approval alone.
Project agent settings, promotion flows and routing bindings are explicitly inactive typed drafts; global
placement produces reusable system templates. Promotion drafts retain their policy
fields, and activation after repository/trust setup uses the existing authorized
`edit_project` command and integration generation fence. Import does not write
active promotion configuration or repository trust anchors.

No signatures or continuing synchronization are part of v1. Copying identical
items is idempotent. Write failures compensate touched files; review failures are
reported with retained draft locations so the operator can retry normal review.

Integration settings are independently selectable. Export captures configured
review/delivery modes, the required engine selector, train policy (including
cadence, repair and admission), repository binding and promotion flow. Repository
ids, branches, required check sets and producers are placeholders; target project
bindings provide defaults. Source activation/artifact identities and task-specific
admission grants are never transferred. Existing target routes are retained;
missing routes must be configured through the normal reviewed activation workflow.
The installation engine selector is a prerequisite, never an imported global change.
Global placement saves a template. Project placement applies active settings through
the supported project configuration path shared with cutover: engine validation,
PolicyActivation generation fencing and repository barriers, plus promotion trust
validation and create-only target preparation. Preview fingerprints include live
settings and generation; changed settings require a new preview and overwrite consent.
No playbook is fabricated or activated by this operation. An absent integration
policy leaves the target's reviewed policy in place. Configuration refusals
occur before writing other selected files; later file/review failures report any
already-configured integration item. Reapplying identical settings is a no-op.

Exported playbook sources and artifacts replace installation, vault, home and
workspace roots with target-install placeholders. Unrecognized absolute machine
paths get explicit placeholders without source defaults. Imports require values
only for selected items, so skipping integration settings or a playbook also skips
its unresolved placeholders. Startup source seeding excludes the retired
supervisor-digest, supervisor-hourly-report and blocked-task-escalation ids; existing
operator files and historical reviewed bundles are retained.

Profiles remain global. Project placement stores agent settings outside the retired
project profile layouts and never creates runtime overrides. Configure the copied
settings through the normal global profile commands when ready; global import
validates and synchronizes profiles while preserving local non-portable settings.

`aq policy diff` supports JSON output. Export/apply and creation with `--from-policy`
are interactive preview workflows and reject `--json` before starting. `--yes`
accepts project defaults but skips system-origin items without an explicit `--scope`;
it never opts into overwrites.

Validation includes typed Handler/API round trips against disposable PostgreSQL,
CLI selection tests, generated-client dashboard interaction tests, and
`node dashboard/layout-checks/playwright-policy-profiles.mjs` against the built SPA
and the isolated HTTP fixture server. The browser check exercises import placement,
skip, overwrite diffs, placeholders, pending-review receipts and exact export/download.
