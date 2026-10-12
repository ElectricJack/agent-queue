# Portable policy profiles (v1)

## Deliberate installation and project setup

Daemon restart does not install, refresh, retire or reactivate playbooks. Existing
vault sources, reviewed bundles and activation choices belong to the operator.
An absent or empty vault is a first installation: install only routing,
provider failover, the usage probe, the default pipeline, failure triage and the
morning report. Morning reporting remains opt-in. Project defaults are currently
implemented by project creation and installer graft, not a separate playbook;
those mechanisms remain unchanged. Agent-type playbooks are not startup defaults.

A receipt outside the vault records this installation and its one-shot activation
attempt. It prevents deletion of even the entire vault from being mistaken for a
new installation. The first activation attempt consumes the receipt before import;
an interruption or invalid bundle requires explicit repair, never repeated seeding.
Existing nonempty vaults need no migration and receive no automatic activations.
Required-playbook reconciliation reads health on ordinary startup. Doctor reports
missing required system activations with an explicit repair command. Reviewed-bundle
refresh and activation repair happen only when the operator requests doctor `--fix`.

The built-in `default` policy is an ordinary `aq.policy.v1` bundle assembled from
shipped reviewed project playbooks and portable settings. CLI creation/onboarding
offers `default`, an exported profile path, or `none`; interactive omission asks,
while JSON/noninteractive omission means none. The dashboard asks the same choice
at review, then opens the existing per-item import dialog. Choosing a profile does
not skip preview, scope selection (project/global/skip), overwrite checks, review,
or activation. Cancelling the import retains the newly created project. No policy
is retroactively applied to existing projects.

Policy profiles copy non-personal project policy once without continuing
inheritance. The local operator exports a
project through `policy_export`, previews exact files, and acknowledges the
preview checksum before writing a directory or `.aqpolicy` zip.

Typed adapters participate: reviewed playbook source and compiled
artifact; agent profile settings and project overrides; promotion flow; CI check
and test-selection policy; routing preferences; and spec, plan and formula
templates; intelligence classes, pool settings and project/repository settings.
Adapters read fixed policy locations and explicit fields. They never
walk arbitrary vault content, serialize operator config, copy memory/history,
account bindings, secrets, GitHub credentials or observed provider availability.
Class definitions and configured pool enabled/min/max/min-per-project/claim limits
accompany routing and profile dependencies. Pool values come from the database,
including overrides newer than the vault file. Derived workers carry their
template/class/harness identity and pool policy, never a flattened profile.
Classes and pools have global runtime scope; project placement retains a draft.
Global placement validates classes and profiles together before any file write,
reconstructs derived stubs and synchronizes their configured pool state.
Project settings include routing bindings, scheduling/budget/review/integration
policy and repository URL/default-branch bindings. Personal Git identities,
Discord/account bindings, repository trust identities, paths and runtime counters
are excluded. Project settings imports remain pending configuration, subject to
the existing authorized project and integration commands.
Enabled system playbooks and referenced profile templates accompany project policy with
system-origin labels. Required playbook dependencies must have reviewed artifacts.
Derived worker profile bodies are regenerated locally from their exported
identity and configured pool policy.

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
