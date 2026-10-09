# Portable policy profiles (v1)

Policy profiles copy non-personal project policy once without continuing
inheritance. The local operator exports a
project through `policy_export`, previews exact files, and acknowledges the
preview checksum before writing a directory or `.aqpolicy` zip.

Only six typed adapters participate: reviewed playbook source and compiled
artifact; agent profile settings and project overrides; promotion flow; CI check
and test-selection policy; routing preferences; and spec, plan and formula
templates. Adapters read fixed policy locations and explicit fields. They never
walk arbitrary vault content, serialize operator config, copy memory/history,
account bindings, secrets, GitHub credentials, provider availability or pool sizes.
Enabled system playbooks and referenced profile templates accompany project policy with
system-origin labels. Required playbook dependencies must have reviewed artifacts.
Derived worker rungs are regenerated locally rather than exported or overwritten.

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
