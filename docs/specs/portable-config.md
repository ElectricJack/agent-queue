# Portable setup configuration

`preview_portable_config`, `export_portable_config` and `import_portable_config`
carry the non-personal configuration needed to recreate an installation.
Version 2 `.aqbundle` archives contain curated YAML, authorable global profiles,
intelligence-class Markdown and typed setup metadata (configured profile/pool
state and project/repository policy). Version 1 archives remain readable.

Every configuration section is reviewed for portable fields, including sessions,
messages, supervisor, worktrees, test selection, reports, knowledge, dashboard,
events, inbox, routing and provider policy. Only credentials, account identities,
machine endpoints/paths and runtime state are omitted. Exclusions are reported
by dotted field name. Known nested omissions do not discard the rest of a section.
Imports recursively merge portable fields with destination-local settings so
replacement never deletes a destination credential, account binding or path.

Pool settings come from `agent_profiles`, rather than trusting stale Markdown.
Derived workers are reconstructed from their template/class/harness identity;
their configured bounds and enabled switch persist in both Markdown and DB.
Classes and all profile inheritance are validated before writes, irrespective
of archive order. Existing customization is kept by default; `replace`, `error`
and dry-run apply to classes, pools, projects, repositories and policy files too.

Projects carry names, scheduling and budget limits, routing bindings, review and
integration preferences, repository URLs/default branches, project overrides,
and fixed policy/template/formula/playbook source locations. Repository test
selection/CI policy and global/project MCP and workspace-kind definitions also
travel. Credentials in URLs,
personal Git identities, Discord IDs, source/checkout/workspace paths, token
counters and live integration state never travel. Repository paths are supplied
on the destination. Integration trust and playbook activation continue to use
their existing explicit setup/review commands; an import does not fabricate
repository trust, review approval or activation receipts.

Archive readers validate declared members, identifiers, bounded reads, typed
metadata, profile/class semantics and portable values before writes. Unexpected
members, traversal, symlinks and unsafe config fields are rejected. Configuration
and profiles containing unknown credential fields fail visibly, rather than
silently exporting them.
