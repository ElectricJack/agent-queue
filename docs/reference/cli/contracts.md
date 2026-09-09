# Command contracts

A **command contract** is the declared surface of one command: typed
arguments, a typed result, the named outcomes it can end in, the capability it
requires, what it changes in the world, whether it is safe to retry, and which
of its fields are secret. Contracts are what a playbook executes commands
through — and, because a contract has a stable fingerprint, what lets the
daemon notice that a command has changed underneath a playbook that was
compiled against an older one.

Contracts are a *stricter* layer than the [tool definitions](agent-tools.md)
that back the CLI and MCP. A tool definition says "here is a JSON Schema for
the arguments". A contract additionally says what the command *does*, in terms
the system can reason about without running it.

Not every command has one. Contracts exist for the commands a playbook can
call; everything else is reached only through the CLI, REST or MCP. The
registry is the authority, so ask it rather than trusting a count in a
document. From a Python shell on the daemon host:

```python
from src.commands.contracts import CONTRACTS
sorted(CONTRACTS.names())        # every contracted command
CONTRACTS.fingerprint("ensure_task")
```

## What a contract declares

Every contract is a `CommandContract`
([`src/commands/contracts/models.py`](../../../src/commands/contracts/models.py))
with an `execution` half the system reasons about and a `presentation` half a
human reads.

### The execution half

| Field | Meaning |
|---|---|
| `name` | The `CommandHandler` command name. |
| `args_model` / `result_model` | Pydantic models with `extra="forbid"` and `frozen=True`. Unknown arguments are an error, not a silent drop. |
| `outcomes` | The named outcomes this command can end in, each classified `success` or `failure`. At least one success is required; `contract_violation`, `unauthorized` and `runtime_error` are reserved and may not be declared. |
| `capability` | The capability name a caller's policy must grant. It may not contain a wildcard. |
| `side_effect` | One of `read`, `create`, `update`, `link`, `resolve`, `composite`. |
| `idempotency` | `none`, `natural` (running it twice is the same as running it once), or `keyed` with the argument that carries the key. |
| `retry_safe` | Whether a caller may retry after an ambiguous failure. |
| `timeout_seconds` | Optional. |
| `effects` | Zero or more *effect clauses* — see below. |
| `sensitive_args` / `sensitive_result_fields` | Field names redacted to `[redacted]` in logs, receipts and explanations. |
| `receipt_projection` | The result fields a durable receipt keeps. |
| `supports_preview` | Whether a preview adapter is registered. |

Every one of those is validated at registration: an idempotency key that is
not an argument, a sensitive field that is not declared, an effect clause
referencing an unknown argument, a duplicate or reserved outcome — each is a
registration error, not a runtime surprise.

### Effect clauses

An effect clause is a machine-readable statement of what the command changes,
so a playbook graph can be *explained* to a human before it runs rather than
after. A clause names a `subject` (a task, a gate, a dependency edge, a
message, an escalation, a branch ownership, …), a `kind`, and an optional
`when` predicate keyed on an argument:

| Kind | Reads as |
|---|---|
| `create` | Creates a new subject. |
| `reuse` | Uses an existing subject, optionally keyed by an argument. |
| `create_or_reuse` | Find-or-create, keyed by an argument. `ensure_task` is the canonical example. |
| `update` | Changes a subject, optionally limited to the fields in one argument. |
| `link` | Relates two subjects named by two arguments. |
| `resolve` | Resolves the subject named by one argument. |
| `read` | Reads only. |

A clause whose kind has no renderer in the playbook explanation layer is
refused at registration, which is what keeps "every step in a playbook graph
can be explained" true by construction rather than by convention.

### The presentation half

`CommandPresentation` carries the title, summary, and human labels for
arguments, outcomes, results and subjects — the strings a dashboard shows next
to a playbook step. It is deliberately excluded from the fingerprint: renaming
a label must not invalidate every artifact compiled against the command.

## Fingerprints and staleness

`execution_fingerprint()` hashes a canonical document of the execution half —
argument and result schemas with presentation-only keys stripped, sorted
outcomes, capability, side effect, idempotency, retry safety, timeout, effect
clauses, sensitive fields, receipt projection and preview support — as
`sha256:<hex>`. `registry_fingerprint()` is the same hash over the whole
registry.

A playbook artifact records the fingerprint of every command it compiled
against. When the daemon evaluates an activation's health
([`src/playbooks/activation.py`](../../../src/playbooks/activation.py)) it
compares those against the live registry and reports:

| Reason | Meaning |
|---|---|
| `command_removed` | A command the artifact calls is no longer registered. |
| `command_contract_changed` | The command still exists but its contract fingerprint moved. |

Either makes the activation `stale_contract`: events that would have triggered
it are *held*, not run, and `aq playbook pending-events` lists them. The fix
is to re-validate and re-activate the artifact against the current contracts —
not to widen anything.

This is why a change to a command's arguments, outcomes or effects is a
semantic event and not an implementation detail: it invalidates the artifacts
that were compiled against the old shape, on purpose.

## The registry

[`CONTRACTS`](../../../src/commands/contracts/registry.py) is the
process-wide `ContractRegistry`. Two properties are worth knowing.

**Registration happens on first read, not on import.** Importing
`src.commands.contracts` registers nothing; the built-ins load the first time
something reads the registry. Registration validates every effect clause
against the playbook explanation renderer, and doing that at import time made
`import src.playbooks.explanation` a circular import. Tests construct a bare
`ContractRegistry()`, which loads nothing.

**A registration is a triple.** `CommandRegistration(name, contract, invoke,
preview=None)` — the contract plus the adapter that actually runs it. The
registry refuses a duplicate name, a registration whose name disagrees with
its contract, and a `preview` adapter that does not exactly match the
contract's `supports_preview`.

Built-ins are registered in three modules:

| Module | Covers |
|---|---|
| [`builtin.py`](../../../src/commands/contracts/builtin.py) | The pipeline commands: task reads and writes, `ensure_task`, gates, dependencies, `message_send`, memory, `git_diff`, `render_prompt`, `stop_task`, `task_route`, `delivery_*`, `task_batch_commit`. |
| [`integration.py`](../../../src/commands/contracts/integration.py) | Every hierarchical-integration primitive — the largest group by far, and the reason most contracts are `composite`. |
| [`escalation.py`](../../../src/commands/contracts/escalation.py) | The durable human-escalation boundary. |

## Redaction

`redact_args` and `redact_result` replace every declared sensitive field with
the literal `[redacted]`. They are applied wherever a command's arguments or
result leave the execution path — logs, durable receipts, playbook
explanations, the dashboard's step view. Declaring a field sensitive is
therefore a one-line change with global effect, which is the point.

## The preview seam

`supports_preview` and `preview_stub`
([`preview.py`](../../../src/commands/contracts/preview.py)) are a
side-effect-free seam for a future dry-run executor. **No built-in registers a
preview adapter today**, so `supports_preview` is false everywhere and the
stub is never reached in normal operation. It exists so that adding dry-run
support is a per-command change rather than a registry redesign.

## What is deliberately *not* in the registry

[`project_onboarding.py`](../../../src/commands/contracts/project_onboarding.py)
sits in this package and uses the same model base classes — frozen,
`extra="forbid"` — but its seven commands are **not registered** in
`CONTRACTS`. That registry is the fingerprinted *pipeline-command* surface;
onboarding is an operator surface gated by the global-admin scope policy in
[`src/api/scope.py`](../../../src/api/scope.py) rather than by a capability
contract. The module also owns the wire shape `onboard_project` accepts: one
flat JSON object discriminated by `source_mode`, validated strictly — a field
belonging to a different mode is an error, not an ignored extra.

## State ownership

| State | Written by | Where it lives |
|---|---|---|
| Contract declarations | Source, at edit time | The three registration modules above |
| The live registry | Process memory, populated on first read | Not persisted |
| Fingerprints an artifact was compiled against | `playbook_v2_propose` / `update_source` at compile time | The immutable playbook artifact |
| Activation health | Computed per evaluation | Not persisted; read with `aq playbook activation-health` |
| Held events | The playbook engine, when an activation is not runnable | `aq playbook pending-events` |

## Common failures and recovery

| Symptom | Diagnose with | What it means |
|---|---|---|
| A playbook stops firing after a code change | `aq playbook activation-health` | `stale_contract` — a command it calls changed shape. Re-validate and re-activate the artifact. |
| Events accumulate with nothing running | `aq playbook pending-events` | The activation is stale, invalid, disabled or its artifact is unavailable. The reason is in the row. |
| `contract <name> is already registered` at startup | The traceback | Two modules registered the same name; only one may. |
| `effect clause '<kind>' has no renderer` | The traceback | A new clause kind was added without teaching the playbook explanation layer to render it. |
| `preview adapter must exactly match supports_preview` | The traceback | A registration declared one and supplied the other. |
| A command runs from the CLI but a playbook step is refused | daemon log, `capability_denied` | The contract's `capability` is not in the calling profile's policy. The CLI ran as the loopback operator, which is not capability-enforced. |

## Related pages

* [The CLI contract](README.md) — the surface a human calls the same commands
  through, and the authority model contracts compose with.
* [Agent-facing tools](agent-tools.md) — the looser tool-definition layer.
* [Command groups](commands.md) — where each contracted command appears on the
  CLI.
* [Module catalog: CLI](../modules/cli.md).

## Source and tests

Models [`src/commands/contracts/models.py`](../../../src/commands/contracts/models.py);
registry [`src/commands/contracts/registry.py`](../../../src/commands/contracts/registry.py);
built-ins [`builtin.py`](../../../src/commands/contracts/builtin.py),
[`integration.py`](../../../src/commands/contracts/integration.py),
[`escalation.py`](../../../src/commands/contracts/escalation.py); preview seam
[`preview.py`](../../../src/commands/contracts/preview.py); onboarding shapes
[`project_onboarding.py`](../../../src/commands/contracts/project_onboarding.py);
explanation renderer
[`src/playbooks/explanation.py`](../../../src/playbooks/explanation.py);
staleness [`src/playbooks/activation.py`](../../../src/playbooks/activation.py).

```bash
aq test tests/test_command_contracts_registry.py tests/test_integration_contracts.py \
        tests/test_project_onboarding_contract.py tests/test_task_routing_contract.py \
        tests/test_playbook_v2_validation.py
```
