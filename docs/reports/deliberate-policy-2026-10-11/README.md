# Deliberate policy daemon acceptance

The isolated daemon acceptance passed on 2026-10-11 for task `calm-vault-68.3`.
Fresh installation produced six sources/bundles and five enabled activations.
Restarts preserved deliberate deletions and activation choices; explicit repair
restored only the requested guarantees. Creation/onboarding retained preview,
review and activation boundaries, including cancellation retention.

Policy and area validation tested implementation HEAD
`0583582235367ba84a8c9e254e96857ccadba656`, based on
`f8e295c342fb2647ede05b65eddbf2244c26e9fa`. The publication adds this evidence to
the tested implementation tree. [Recorded file hashes and observations](policy-evidence.json)
identify the tested code independently of the evidence commit.

## Isolation and reproduction

The policy kit used `/tmp/aq-e2e-calm-vault-68-3`, parent port `53989`, and database
`agent_queue_e2e_calm_vault_68_3`. Four marked child homes under `policy-cases/`
used dynamically chosen ports and database suffixes `_policy_absent`,
`_policy_empty`, `_policy_existing` and `_policy_failed`. Every daemon was a
separate process backed by disposable PostgreSQL. Fake sessions, disabled
provider probes, a direct LLM rejection hook and the local notification sink
bounded traffic. Every child stop checked that no LLM attempt was recorded.

```bash
export AQ_E2E_HOME=/tmp/aq-e2e-calm-vault-68-3
export AQ_E2E_PORT=53989
export E2E_DB_NAME=agent_queue_e2e_calm_vault_68_3
export AQ_E2E_TMUX_SOCKET=aq-e2e-calm-vault-68-3
scripts/e2e-env.sh --reset
scripts/e2e-smoke.sh
scripts/e2e-policy.sh
```

The builder explicitly installed policy before adding fixture profiles/formulas.
Each scenario recorded that installation separately, then prepared its startup
state. Preservation restarts invoked only `e2e-daemon.sh`. Fixture SQL and file
deletion were guarded to the marked child homes and disposable database names.

## Observed policy behavior

The exact installed IDs were `default-assignment-routing`, `provider-usage-probe`,
`provider-failover`, `supervisor-failure-triage`, `default-pipeline` and
`morning-report`. All except `morning-report` activated initially. The receipt
remained outside the vault with `activation_pending: false` after the attempt.

| Scenario | Sources and bundles after restart | Activation observation |
|---|---|---|
| Absent vault, absent receipt | Six of each | Five enabled, healthy rows |
| Empty vault, absent receipt | Six of each | Five enabled, healthy rows |
| Delete usage-probe source, retain receipt | Five sources, six bundles | Exact original rows preserved |
| Delete morning-report bundle, retain receipt | Six sources, five bundles | Exact original rows preserved |
| Delete whole vault, retain receipt | Zero sources and bundles | Exact original rows preserved |
| Existing nonempty vault, absent receipt | Operator source and deliberately stale bundle preserved | Zero activations; consumed receipt |
| Missing failover, disabled usage probe, stale router | Fixture state preserved by restart and read-only doctor | Live health reported missing, disabled and stale |
| Explicit required repair | Optional drift/removal and authored source preserved | Three required enabled; missing optional triage stays absent; pipeline stays disabled |
| Explicit installed-bundle repair | Drift refreshed; superseded bytes backed up; operator note retained; morning-report stays absent | Healthy required rows and optional choices preserved |
| Failed initial router import, valid bytes restored, restart | Receipt remains consumed | Four independent initial rows preserved; router still absent |
| Explicit repair after consumed failure | Receipt bytes unchanged | Router added; four independent rows preserved |

The absent-vault router activation was
`3befa53e-a135-4917-b30b-8135447f9a07`, usage probe
`c56c962d-a682-4313-bd4e-765fb632cbbd`, and initial failover
`acfa4929-a71b-4320-b7ee-00a17eb5e38d`. Repair kept router/probe identities and
replaced the deliberately deleted failover row with
`22ce5f83-b098-439d-ad79-453f1c4aaa54`. All before/after source and bundle hashes,
full activation rows, doctor findings and command statuses are in the JSON.
Generated Obsidian hub files outside bundle directories are excluded from policy
byte comparisons; every file inside each bundle is compared.

Both `project create` and `project onboard` exercised `default`, an exported
archive, `none`, noninteractive omission and cancelled default import. Export
preview left no archive until checksum acknowledgement. Import showed item
preview and scope selection, then required acknowledgement. Default policy
created two pending playbook reviews; approving both stored them without changing
activations. Exported settings were staged without activation. Cancel exited 1
while retaining the new project. Existing project database rows and file hashes
remained unchanged. Creation uses its code-owned defaults; installer graft code
is unchanged.

## Startup correction and validation

The initial real absent-vault run at base HEAD installed the six bundles but
activated only four policies. Its router import failed with `unknown_profile`
for `playbook-compiler`: early reconciliation consumed the one-shot receipt before
profile synchronization. The correction makes early startup observe health and
leaves the existing late reconciliation as the sole initial attempt after
registries are populated. It adds no restart retry. Two absent/empty-vault
Orchestrator regression cases pass, and both real-daemon cases activate all five.
[Initial failure observations](startup-failure.json) preserve the reproduction.

The final area command was:

```bash
aq test --aq-detach --aq-wait --aq-idempotency-key calm-vault-68.3-final-area-v1 \
  tests/test_orchestrator.py tests/test_required_playbooks.py \
  tests/test_policy_profiles.py tests/test_cli_policy.py \
  tests/test_e2e_kit_fixtures.py tests/test_e2e_probe.py \
  tests/test_e2e_cli_stateful.py tests/test_script_modes.py \
  tests/test_selection_catalogue.py
```

The daemon supplied its disposable test DSN on `127.0.0.1:55154`; AQ retained
six workers and default marker exclusions. Job
`e29d7a0a-0923-47e5-bfca-1f1c695d3ab2` passed **427 tests**, exit 0, with 40
warnings in 123.62 seconds. Result hash:
`32280d026407f9168025e7057c1c18752f1ab48e1790c1c98eaf74a6d59622a2`.
Retrieve the result with `aq job result <job-id> --json` and retained output with
`aq job logs <job-id>`.

The policy runner exited 0. Full raw command/HTTP evidence remains at
`/tmp/aq-e2e-calm-vault-68-3/policy-acceptance.json` (794519 bytes, SHA256
`30a38bc04cd420750f62235c54f96ad8c1158924f99d5f7c5dac81f31a30b6f9`).
The tracked JSON retains observations, command/status records and transcript
hashes while omitting repeated preview bodies. Snapshot `*_ref` fields identify
shared `inventory_sets` and `activation_sets` by their content hash.

The final smoke used a separately reset kit at
`/tmp/aq-e2e-calm-vault-68-3-final-smoke`, port `53990`, database
`agent_queue_e2e_calm_vault_68_3_final_smoke`. Both `scripts/e2e-env.sh --reset`
and `scripts/e2e-smoke.sh` exited 0; **all 20 scenarios passed**. The smoke exercised
the same production and smoke-script bytes as the recorded implementation HEAD.
Its [complete transcript](smoke.txt) retains the surface inventory: S20 passed
source provenance, with train delivery explicitly untested because `git_first`
was inactive. Real providers, tmux and external messaging belong to Tier 2.

The recorded baseline is the operator-vault note
`projects/agent-queue/notes/full-suite-baseline-2026-09-22.md`, main `35d9605e`,
with 45 default-suite failures. This bounded validation encountered none.
An initial foreground test invocation without `POSTGRES_TEST_DSN` ran no tests;
the configured disposable test server resolved it. Ruff, shell syntax, changed
Markdown links and selection-catalogue regeneration passed.
