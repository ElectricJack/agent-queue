# Retired factory playbooks

`hierarchical-delivery`, `root-integration-train`, and
`memory-consolidation` were retired on 2026-09-11. They are no longer shipped,
seeded, or reviewed for import, and are not valid choices for a new project
policy. Do not import or activate them.

Frozen routes, artifacts, and run history remain evidence. A local operator
must use guarded integration commands to end or hold every referenced operation
before deleting the exact disabled catalog entries. Do not delete artifact
files, branches, or run records directly.

First inspect the affected project and playbooks:

```bash
aq integration status <project-id>
aq playbook activation-health --playbook-id hierarchical-delivery
aq playbook activation-health --playbook-id root-integration-train
aq playbook activation-health --playbook-id memory-consolidation
aq playbook list-runs --playbook-id hierarchical-delivery --status running
aq playbook list-runs --playbook-id root-integration-train --status running
aq playbook list-runs --playbook-id memory-consolidation --status running
```

For each operation still using a retired route, use the existing guarded
integration disposition chosen from fresh status; for example,
`aq integration abort <operation-id> --reason 'retire obsolete integration playbook'`.
The command must preserve live ownership, branches, and historical evidence.

Only after every reference is ended or visibly held and the activation is
disabled, delete the exact catalog entries as the local operator:

```bash
aq playbook delete --playbook-id hierarchical-delivery --scope system --scope-identifier '' --artifact-sha256 579b8a1a92b66d885acba6417483e5426f2bab6691c55bd8810ee3bd087a4d82
aq playbook delete --playbook-id root-integration-train --scope system --scope-identifier '' --artifact-sha256 a93c3c32e05bdff64f25279531b6eaf04aba01e8bbd48a8125230a1ca32fa160
aq playbook delete --playbook-id memory-consolidation --scope system --scope-identifier '' --artifact-sha256 536f2ba440e2c5a2641948135f846556bda6ec9b936b5b0913d76043daf7e166
```

The delete command itself refuses active or referenced work. That refusal is a
safety result: retain the evidence and resolve the named operation rather than
forcing deletion.

Before deleting `memory-consolidation`, verify the pre-existing exact backup:

```bash
backup=$(mktemp -d)
tar -xzf ~/.agent-queue/vault/backups/policy-retirement-2026-09-11/memory-consolidation-exact.tar.gz -C "$backup"
sha256sum "$backup/vault/system/playbooks/memory-consolidation.md" "$backup/compiled/artifacts/536f2ba440e2c5a2641948135f846556bda6ec9b936b5b0913d76043daf7e166.json"
```

The required hashes are respectively
`808ee5daafed43bc936d6a43be6132e2fa65f161ba0395ce6dfaa44590d06b69` and
`536f2ba440e2c5a2641948135f846556bda6ec9b936b5b0913d76043daf7e166`.
