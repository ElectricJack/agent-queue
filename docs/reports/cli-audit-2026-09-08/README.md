# aq CLI audit — final acceptance, 2026-09-09

The final audited and accepted revision is the collected aggregate head
`bd054e4129b2a66a859408f0fa2b76b62a5d670e`. The runtime baseline for the tables
and command transcripts below is `5d8bc501986909256c7b1e45857f3eaf0043eb97`,
which includes the pre-report repair integration candidate
`d6d7bede968033780c845d903e454ce46b32178f`, the generated inventory acceptance
metadata, and the `--aq-all-markers` wrapper correction found during final
verification. Every confirmed defect in the 2026-09-08 audit is corrected or
deliberately dispositioned, and the focused suite at that baseline passed 574
tests. The disposable-daemon acceptance suite then passed all 14 stateful
scenarios and removed its temporary database, repositories, vault, plugin
fixture, and daemon home. Changes after that baseline are **not**
documentation-only: two runtime repairs and one formatting change to
`src/cli/` landed on top of it. Their content and the acceptance evidence
re-collected at the aggregate head are recorded in
[Re-verification of the collected aggregate](#re-verification-of-the-collected-aggregate).

This is not a claim that all 317 current leaf commands were executed against a
live backend. The maintained [machine-readable inventory](../../reference/cli-command-inventory.json)
labels every current leaf conservatively and keeps removed audit surfaces in a
separate historical ledger. Registration, help, or mock dispatch alone produces
`untested`, never `working`.

## Environment and inventory

- Audit date: 2026-09-09 (America/Los_Angeles).
- Final audited/accepted revision: the collected aggregate head
  `bd054e4129b2a66a859408f0fa2b76b62a5d670e`; runtime baseline for the tables
  and transcripts below: `5d8bc501986909256c7b1e45857f3eaf0043eb97`; pre-report
  integration candidate: `d6d7bede968033780c845d903e454ce46b32178f`; original
  audit revision: `86c87a99a21385a25a393b4d0c4b5d5552b0933e`.
- CLI version: 0.1.0. Linux 6.18.33.2-microsoft-standard-WSL2 x86_64;
  Python 3.12.3; pytest 9.0.3.
- `aq test` used one of two global slots and the configured three-worker cap.
- Focused tests used the repository's disposable PostgreSQL 18 service at
  loopback port 5533. The stateful run created and dropped its uniquely named
  database and temporary filesystem resources. `AQ_DB_SCOPE=worker` and the
  operator-database refusal sentinels were not changed.
- The system-Python installation exposed `aq-memory` 0.1.0 as the `memory`
  `aq.plugins` entry point. It adds no top-level leaf because the core CLI already
  owns `aq memory`; the two schema-advertised memory leaves are labeled
  `external-plugin` / `aq-memory`. The disposable suite also installed an
  `e2e-fixture ping` extension, proved that it loaded when present, and proved it
  was an exit-2 unknown command when absent.

Current-leaf status totals are 55 `working`, 0 `broken`, 1 `obsolete`,
0 `unsupported`, and 261 `untested`. The six historical rows add five obsolete
spellings and one deliberately unsupported removed operation. The JSON inventory
contains the status, provider, backend, parameter contract, and exact evidence
tag for each command. Its companion [inventory guide](../../reference/cli-command-inventory.md)
defines the categories and regeneration contract.

## Original findings and final disposition

| Original finding or report row | Final status | Evidence / operator action |
| --- | --- | --- |
| Partial `task create` flags restarted the full wizard and lost values | Resolved | Focused partial project/title/description, cancellation, non-TTY, type, integration mode, and priority tests passed. Disposable scenario S9 proved an incomplete noninteractive request exits 2 without persistence. |
| Explicit priority zero became 100; bounds were not enforced | Resolved | Click and wizard paths enforce 1–300 and preserve the default 100. S9 proved priority 0 fails and leaves no task. |
| Single-task `--json` success printed human text | Resolved | Success, brief, markup, multiline, error, and one-request tests passed. S9 extracted the created ID from the JSON envelope and read the persisted task. |
| Success/error/brief JSON differed across command families | Resolved | Envelope tests cover handwritten, generated, plugin-wrapper, list/object/scalar, Unicode, usage, auth, connection, and command failures. `AQ_JSON_LEGACY=1` remains the documented compatibility escape hatch. |
| Global flags worked only before the command | Resolved | Prefix, group, trailing, mixed, duplicate, nested, `--` boundary, and passthrough cases passed; `aq status --json` is a regression case. |
| `task create` could not express `requires_kinds` | Resolved | Repeatable `--requires-kind KIND[=ALIAS]` is documented and tested for parsing, request forwarding, persistence, unknown kinds, graph/from-spec refusal, omission, and parity. S9 persisted and returned `project-repo`. |
| Import/help opened the plugin database and could hang | Resolved | Offline subprocess tests prove help, help-all, version, schema, and test help do not initialize a DB or require a daemon. Plugin config reads are lazy, bounded, read-only, and visible on failure. |
| `aq task ask-human` was advertised without an implementation | Unsupported by design; removed | The Click/schema/API-client surface no longer advertises it. Questions are correlated to harness transcripts and existing question identity; workers report blockers with `aq message send`. No second state machine was added. |
| `aq plugin logs NAME` looked like working history retrieval | Obsolete compatibility stub | Human and JSON contract tests identify it as unavailable and direct operators to `aq playbook list`; it never fabricates empty history. |
| Stale task spellings and close outcomes in skills | Resolved / obsolete | Use `get-tree`, `get-result`, `add-dependency`, `remove-dependency`, and `pass|fail`. The historical inventory preserves the removed spellings and the CLI module/auth/profile guidance is checked for drift. |
| Mock dispatch allowed missing or plugin-owned handlers to be misclassified | Resolved | The generated inventory distinguishes core, internal-plugin, external-plugin, and installed extension ownership. A new generated operation without a handler/provider/explicit disposition fails generation. |
| Missing test DSN caused hundreds of fixture errors; scratch DBs collided | Resolved | Missing DSN now fails once before a slot is taken with setup guidance. Each run receives an ownership token and unique databases; cleanup refuses resources it does not own. The initial final-audit command reproduced the clean preflight before the DSN was supplied. |
| Stateful mutations had only help or mocked dispatch evidence | Resolved conservatively | The 14-scenario disposable suite covers representative task/project/workspace/file/git/note/message/MCP/graph/vault/formula/session/pool flows and rollback/refusal paths. All remaining commands stay `untested` in the per-command inventory. |
| Graph creation was rejected by the atomic hierarchy guard | Resolved | The repair uses atomic hierarchy filing; focused integration tests cover new/existing parents, ordinals, rollback, and policy. The guard remains enabled. |
| New root creation resolved its not-yet-created branch | Resolved | Root origins bootstrap from a validated existing default ref; rollback and retry semantics are covered without weakening ownership fences. |
| Restart after a possibly accepted write encouraged unsafe replay | Resolved | Lost/interrupted HTTP responses return an `unknown_outcome` error and do not prompt a restart/retry. Focused tests cover JSON/human paths and verify exactly one request. |

The original area table is reconciled by the generated per-command artifact, not
by promoting whole families. For example, the exercised MCP registry commands
are `working`, while unexercised MCP operations remain `untested`; the same rule
applies to task, project, agent, session, playbook, pool, system, integration,
stream, database, and plugin-management families.

## Exact verification

The first focused invocation intentionally omitted `POSTGRES_TEST_DSN` and exited
before collection with one actionable setup error. No test ran and it is not
counted as a pass. After starting only the repository's disposable PostgreSQL
service, the following checks were run:

```bash
python scripts/generate-cli-command-inventory.py
python scripts/generate-cli-command-inventory.py --check

POSTGRES_TEST_DSN='postgresql+asyncpg://agent_queue:agent_queue_dev@localhost:5533/postgres' \
  aq test tests/test_cli.py tests/test_cli_menus.py \
  tests/test_cli_task_create_output.py tests/test_cli_task_create_requires_kinds.py \
  tests/test_cli_envelope.py tests/test_cli_global_options.py \
  tests/test_cli_startup_offline.py tests/test_cli_inventory.py \
  tests/test_cli_conformance.py tests/test_cli_response_failures.py \
  tests/test_cli_plugins.py tests/test_command_surface.py \
  tests/test_guidance_docs.py tests/test_cli_module_map.py
# 574 passed, 19 warnings in 34.01s

POSTGRES_TEST_DSN='postgresql+asyncpg://agent_queue:agent_queue_dev@localhost:5533/postgres' \
  PYTHONPATH=. aq test --aq-all-markers -p no:xdist -s \
  tests/test_e2e_cli_stateful.py
# 1 passed, 2 warnings in 313.19s; inner smoke assertion: 14/14 scenarios passed

python scripts/generate-cli-command-inventory.py --check
ruff check src/cli/inventory.py src/cli/test_runner.py \
  tests/test_cli_inventory.py tests/test_cli_test_runner.py

POSTGRES_TEST_DSN='postgresql+asyncpg://agent_queue:agent_queue_dev@localhost:5533/postgres' \
  PYTHONPATH=. aq test tests/test_cli_inventory.py \
  tests/test_cli_test_runner.py tests/test_guidance_docs.py
# 62 passed, 8 warnings in 14.43s

POSTGRES_TEST_DSN='postgresql+asyncpg://agent_queue:agent_queue_dev@localhost:5533/postgres' \
  PYTHONPATH=. aq test tests/test_cli*.py tests/test_command_surface.py \
  tests/test_guidance_docs.py
# 811 passed, 19 warnings in 70.30s
```

The final run also exposed and fixed one audit-gate defect: `--aq-all-markers`
previously omitted the wrapper's marker expression but allowed `pyproject.toml`
to apply the same deselection, so the integration test was still excluded. It
now passes an empty command-line marker expression, which overrides configured
addopts. The pre-fix reproduction exited 5 with one deselected and zero selected.

## Re-verification of the collected aggregate

Three commits landed between the `5d8bc501` runtime baseline and the collected
aggregate head `bd054e41`. Two of them change shipped CLI code, so the earlier
claim that everything after the baseline was a documentation-only provenance
correction was wrong and has been withdrawn.

| Commit | Change | Runtime files |
| --- | --- | --- |
| `0ec19571` | fix(cli): make `aq plugin logs` an explicit removal, not a quiet no-op | `src/cli/plugins.py` (+51/-9), plus 74 lines of contract tests in `tests/test_cli_plugins.py` and a `docs/specs/plugin-system.md` update |
| `64066bb9` | fix(cli): advertise the runnable form of `aq playbook inspect-run` | `src/cli/plugins.py` (+1/-1), plus 34 lines of tests |
| `bd054e41` | style(cli): format integrated inventory metadata | `src/cli/inventory.py` (+1/-3, formatting only) |

The audit gates were therefore re-collected at `bd054e41` rather than inherited
from `5d8bc501`:

- `python scripts/generate-cli-command-inventory.py --check` — clean, 317 leaf
  commands, matching the totals reported above.
- `ruff check --select E4,E7,E9,F` over all 70 changed `.py` files — clean.
- `aq test tests/test_cli*.py tests/test_command_surface.py tests/test_guidance_docs.py`
  — 820 passed. The baseline run reported 811; the extra nine come from the
  `aq plugin logs` removal contract and the `inspect-run` advertising fix.
- `aq test` over fifteen other touched areas, including
  `tests/test_api_client_contract.py` — 701 passed, 12 skipped.
- `aq test --aq-all-markers -p no:xdist -s tests/test_e2e_cli_stateful.py`
  — 1 passed; inner assertion 14/14 stateful scenarios passed.

Scope of that evidence: these are the CLI-audit gates, re-run locally at the
aggregate head. They are not a full-suite result. The hosted CI run on the
snapshot of `bd054e41` was red — including
`tests/test_e2e_cli_stateful.py::test_disposable_daemon_stateful_cli_smoke`,
which is `integration`-marked and fails on hosted runners because
`scripts/e2e-common.sh` defaults `E2E_PG_PORT` to this workstation's disposable
service port. That failure is an environment-portability defect in this epic's
own harness, tracked separately; it is not a CLI-behavior finding and does not
change any per-command disposition above.

## Explicit limitations

The Tier-1 suite does not contact paid model providers, Milvus/Ollama, real MCP
servers, Discord, webhooks, remote plugin repositories, or arbitrary Git hosts.
It does not launch real tmux harnesses, activate/run playbooks, install/update/remove
remote plugins, migrate an operator database, or start/stop the operator daemon.
Those paths are marked `dependency-unavailable` or `explicitly-untested` by the
smoke capability report and remain `untested` per command.

`aq-memory` execution remains dependency-unavailable in this environment even
though ownership and discovery are verified. The MCP probe deliberately targets
closed loopback port 1 and is accepted only as an unavailable-dependency result.
Messages use a database-only sink recipient; no external delivery is claimed.

No Critical, High, Medium, or Low unresolved CLI finding remains from this gate.
The conservative `untested` inventory is an explicit coverage boundary, not a
claim of breakage and not grounds to advertise those commands as accepted.
