# GitHub access refactor: filed AQ task index

Filed through the AQ CLI on 2026-09-22 in project `agent-queue` for [issue #615](https://github.com/ElectricJack/agent-queue/issues/615).

Design: [GitHub access specification](../../specs/github-access.md). Execution snapshot: `projects/agent-queue/specs/github-access-615.md` in the AQ vault. The snapshot remains a draft because these tasks were filed directly; approving it through spec ingest would risk creating duplicate work.

## Dependency chain

`smart-beacon → sharp-stone → brisk-horizon → clear-meadow → fair-bridge`

Each later epic and every child within it has a blocking dependency on the previous epic. Additional child dependencies below allow safe parallel work within an epic. There are 5 epic containers and 20 executable child tasks.

| Epic | Scope | Children | Blocked by |
| --- | --- | ---: | --- |
| `smart-beacon` | shared credentials and gh runner | 4 | None |
| `sharp-stone` | shared repository client and integration | 3 | `smart-beacon` |
| `brisk-horizon` | PR, CI and account command migration | 4 | `sharp-stone` |
| `clear-meadow` | authenticated Git and worker delivery | 5 | `brisk-horizon` |
| `fair-bridge` | remove legacy paths and verify App/PAT parity | 4 | `clear-meadow` |

## Child tasks

### smart-beacon: shared credentials and gh runner

| Task | Deliverable scope | Blocking prerequisites |
| --- | --- | --- |
| `smart-beacon.1` | Publish the GitHub access spec and define shared auth/binding contracts | None |
| `smart-beacon.2` | Extract App bootstrap and implement unified credential selection | `smart-beacon.1` |
| `smart-beacon.3` | Implement the single isolated gh subprocess runner | `smart-beacon.1` |
| `smart-beacon.4` | Compose shared authentication and repository binding safely | `smart-beacon.2`, `smart-beacon.3` |

### sharp-stone: shared repository client and integration

| Task | Deliverable scope | Blocking prerequisites |
| --- | --- | --- |
| `sharp-stone.1` | Implement shared repository API responses and bounded pagination | `smart-beacon` |
| `sharp-stone.2` | Move integration repository operations onto the shared client | `sharp-stone.1`, `smart-beacon` |
| `sharp-stone.3` | Wire shared GitHub services and preserve credential-based trust policy | `sharp-stone.2`, `smart-beacon` |

### brisk-horizon: PR, CI and account command migration

| Task | Deliverable scope | Blocking prerequisites |
| --- | --- | --- |
| `brisk-horizon.1` | Migrate ordinary PR, polling and CI operations to the shared client | `sharp-stone` |
| `brisk-horizon.2` | Unify onboarding, repository creation and GitHub capability reporting | `sharp-stone` |
| `brisk-horizon.3` | Route profile gist sharing through the common capability boundary | `brisk-horizon.2`, `sharp-stone` |
| `brisk-horizon.4` | Preserve PR mutation reconciliation and partial-merge outcomes | `brisk-horizon.1`, `sharp-stone` |

### clear-meadow: authenticated Git and worker delivery

| Task | Deliverable scope | Blocking prerequisites |
| --- | --- | --- |
| `clear-meadow.1` | Authenticate isolated clone, fetch and immutable PR validation | `brisk-horizon` |
| `clear-meadow.2` | Unify authenticated branch push and deletion with exact leases | `clear-meadow.1`, `brisk-horizon` |
| `clear-meadow.3` | Migrate integration, recovery and cleanup transfer callers | `clear-meadow.2`, `brisk-horizon` |
| `clear-meadow.4` | Expose scoped daemon-backed worker delivery and lease parameters | `clear-meadow.3`, `brisk-horizon` |
| `clear-meadow.5` | Update worker instructions and grants for credential-free delivery | `clear-meadow.4`, `brisk-horizon` |

### fair-bridge: remove legacy paths and verify App/PAT parity

| Task | Deliverable scope | Blocking prerequisites |
| --- | --- | --- |
| `fair-bridge.1` | Remove legacy GitHub transports and enforce one execution path | `clear-meadow` |
| `fair-bridge.2` | Document unified GitHub setup, limitations and upgrade behavior | `fair-bridge.1`, `clear-meadow` |
| `fair-bridge.3` | Build automated private-repository workflow and credential parity tests | `fair-bridge.1`, `clear-meadow` |
| `fair-bridge.4` | Run disposable GitHub acceptance and record refactor completion evidence | `fair-bridge.2`, `fair-bridge.3`, `clear-meadow` |

## Inspection and verification

All five graphs passed `aq task create --graph - --dry-run` without warnings before creation. The saved hierarchy, parent-to-parent blocking edges, and every child's blockers, specification references, deliverables, and acceptance criteria were read back through the CLI. All children use the `standard-high` intelligence class without pinning a provider.

The project's normal scheduler started `smart-beacon.1` after filing; task statuses are live and should be inspected through AQ:

```bash
aq task show smart-beacon
aq task children --task-id smart-beacon --limit 100
aq task deps --task-id fair-bridge
aq task show fair-bridge.4
```

[Planning manifest](tasks.plan.json) records the detailed decomposition. It is a multi-epic planning document, not an AQ graph to submit directly. [Filing receipt](filed-tasks.json) records the actual IDs and dependencies returned by creation. These tasks already exist; do not resubmit the plan.

Live acceptance must use an explicitly provisioned disposable private repository and App installation. Missing fixtures remain an unmet acceptance item; production repositories and mock-only evidence do not satisfy that gate.
