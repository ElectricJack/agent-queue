# Response models

Every typed route on the [HTTP API](README.md) declares what it answers with.
Those declarations live in [`src/api/models/`](../../../src/api/models/) as
Pydantic models, they become the schemas in
[`openapi.json`](../../../openapi.json), and both generated clients are built
from them. This page says how a command, a model, a schema and a client class
line up, and which module owns which family.

## The chain from a command to a client class

```mermaid
flowchart LR
  CMD["command<br/>task_show"] --> ROUTE["POST /api/task/show<br/>operationId: task_show"]
  REQ["command descriptor / explicit request model"] --> ROUTE
  RESP["TaskShowResponse<br/>src/api/models/task.py"] --> ROUTE
  ROUTE --> SPEC["openapi.json<br/>TaskShowRequest / TaskShowResponse"]
  SPEC --> PY["agent_queue_api_client<br/>models/task_show_response.py"]
  SPEC --> TS["@aq/ts-client<br/>types.gen.ts"]
```

The naming is mechanical, which is what makes the generated clients navigable:

| Thing | Name |
|---|---|
| Command | `task_show` |
| Path | `POST /api/task/show` (category, then the command with `_` → `-`) |
| `operationId` | `task_show` |
| Request schema | `TaskShowRequest` — an explicit request-model override, or generated from the command descriptor's JSON input schema by [`_make_input_model`](../../../src/api/codegen.py) |
| Response schema | `TaskShowResponse` — a hand-written model in `src/api/models/` |
| Error schema | `TaskShowResponse422` |
| Python client | `agent_queue_api_client.api.task.task_show`, models `TaskShowRequest` / `TaskShowResponse` |
| Nested objects | `TaskShowResponseContextItem`, `TaskShowResponseParentType0`, … — the generator names anonymous sub-objects after their parent |

A command has a response model when
[`get_all_response_models()`](../../../src/api/models/__init__.py) finds one
for its name. The registry is the union of the `RESPONSE_MODELS` dict each
category module exports.
[`tests/test_response_model_registry.py`](../../../tests/test_response_model_registry.py)
fails when a newly categorized command has no model, because the generated
TypeScript client would type that call's result as `unknown` and the dashboard
would silently lose it. A short, explicitly listed set of commands is exempt
because they return an unstructured dictionary on purpose.

## What is in each module

The table groups models by source module. For the current registry, use
`_category_modules()` in `src/api/models/__init__.py`; file and class counts
change whenever the public contract grows.

| Module | Family |
|---|---|
| [`__init__.py`](../../../src/api/models/__init__.py) | `ErrorResponse`, `TaskRef`, `TaskBrief`, and the registry |
| [`agent.py`](../../../src/api/models/agent.py) | Agents, profiles, sessions-as-agents, questions, pools |
| [`ask.py`](../../../src/api/models/ask.py) | Versioned source decisions and durable application results |
| [`collaboration.py`](../../../src/api/models/collaboration.py) | Shared collaboration request and response contracts |
| [`cron.py`](../../../src/api/models/cron.py) | Recurring prompt schedules |
| [`dashboard.py`](../../../src/api/models/dashboard.py) | Durable dashboard state documents and revision conflicts |
| [`decision.py`](../../../src/api/models/decision.py) | Operator decision records and history |
| [`delivery_latency.py`](../../../src/api/models/delivery_latency.py) | Computed delivery latency report and time breakdown |
| [`digest.py`](../../../src/api/models/digest.py) | Digest preview and schedule health |
| [`discord.py`](../../../src/api/models/discord.py) | Channel housekeeping |
| [`escalation.py`](../../../src/api/models/escalation.py) | Human escalations and their replies |
| [`files.py`](../../../src/api/models/files.py) | Read, write, edit, glob, grep, notes |
| [`gate.py`](../../../src/api/models/gate.py) | Human and routing gates |
| [`git.py`](../../../src/api/models/git.py) | Status, diff, log, branch, push, PR, merge |
| [`graph.py`](../../../src/api/models/graph.py) | The aggregate project-graph read |
| [`graph_layout.py`](../../../src/api/models/graph_layout.py) | Requests *and* responses for the spatial layout routes |
| [`integration.py`](../../../src/api/models/integration.py) | Author-assisted integration close continuations |
| [`job.py`](../../../src/api/models/job.py) | Managed job request, result, logs, cancellation and retention contracts |
| [`knowledge.py`](../../../src/api/models/knowledge.py) | Knowledge records and their stable result envelopes |
| [`mcp.py`](../../../src/api/models/mcp.py) | MCP server registry and tool catalog |
| [`memory.py`](../../../src/api/models/memory.py) | Semantic memory search, save, profiles |
| [`message.py`](../../../src/api/models/message.py) | Inbox, send, reply, threads |
| [`metrics.py`](../../../src/api/models/metrics.py) | One fleet metrics sample and a series of them |
| [`object_loop.py`](../../../src/api/models/object_loop.py) | Durable object evaluation results |
| [`operator.py`](../../../src/api/models/operator.py) | Scoped operator execution, preflight and audit references |
| [`playbook.py`](../../../src/api/models/playbook.py) | Playbook listing, health, artefacts, run history |
| [`playbook_v2.py`](../../../src/api/models/playbook_v2.py) | The V2 semantic-graph surface: definitions, proposals, validation, runs |
| [`plugin.py`](../../../src/api/models/plugin.py) | Plugin list, install, enable, config |
| [`policy.py`](../../../src/api/models/policy.py) | Policy profile preview, placement, import and export receipts |
| [`project.py`](../../../src/api/models/project.py) | Projects, workspaces, workspace kinds, constraints |
| [`project_onboarding.py`](../../../src/api/models/project_onboarding.py) | Root browsing, GitHub discovery, the onboarding saga |
| [`provider.py`](../../../src/api/models/provider.py) | Provider quota as each provider reports it |
| [`records.py`](../../../src/api/models/records.py) | Record reads, search, links and capabilities |
| [`report.py`](../../../src/api/models/report.py) | Supervisor report reconciliation and retrieval |
| [`review.py`](../../../src/api/models/review.py) | Document review submission, revisions and refusals |
| [`session.py`](../../../src/api/models/session.py) | Session listing, logs, input, lifecycle |
| [`supervisor_inbox.py`](../../../src/api/models/supervisor_inbox.py) | Global supervisor conversation history, status and explicit replies |
| [`system.py`](../../../src/api/models/system.py) | Config, schema, logs, events, costs, doctor, integration control |
| [`task.py`](../../../src/api/models/task.py) | Tasks, comments, gates, dependencies, claims, batches |
| [`test_selection.py`](../../../src/api/models/test_selection.py) | Explicit smart test selection reports |
| [`wait.py`](../../../src/api/models/wait.py) | Wait request overrides, receipts and persisted explanations |

`graph_layout.py` and `metrics.py` are consumed directly by resource routes;
`delivery_latency.py` supplies report shapes used directly by `metrics.py`.
Category modules can also export `REQUEST_MODELS` for explicit nested input
validation, aggregated by `get_all_request_models()`.

## Conventions inside the models

* **Optional with a neutral default.** A sample or a listing is often
  deliberately partial — a host with no `/proc/meminfo`, a tier that has not
  rolled up yet — and a strict model would turn an honest gap into a `500`.
  The metrics models state this rule explicitly and follow it field by field.
* **Errors are separate schemas.** Every generated route declares a `422`
  alongside its success model, so the generated clients can parse a failure
  rather than throw. Families in `DETAILED_ERROR_COMMANDS` retain structured
  refusals, including
  jobs, waits, asks, reviews, knowledge and dashboard state. Command-specific
  status maps also declare conflicts and other errors; see
  [error envelopes](conventions.md#the-two-error-envelopes).
* **Absent versus null.** Responses serialize `None` as `null` by default. One
  command opts out — `playbook_graph_view` is serialized with
  `exclude_none`, because its compiled node details model "not set" as an
  absent key, and rendering them as `null` would fill the dashboard's node
  inspector with empty rows.
* **Inheritance is visible in the client.** `TaskShowResponse` extends
  `TaskDetail`; the generator flattens that into one class with every field,
  so the client class is bigger than the model file suggests.

## Changing a model

1. Edit the model in `src/api/models/`.
2. Regenerate the artifacts, from the repository root:

   ```bash
   ./scripts/regenerate-api-client.sh --offline
   ./scripts/regenerate-ts-client.sh --from-file
   ```

3. Commit `openapi.json` and `packages/aq-client/` with the source change.

Skipping step 2 is caught rather than shipped:
`tests/test_api_client_contract.py::test_committed_openapi_json_matches_the_live_app_surface`
compares the committed spec with the one `create_app()` serves. Never hand-edit
anything under `packages/aq-client/` — see the
[Python client](python-client.md) page for why that tree is a pure function of
the spec and a pinned generator.

## State ownership

These models describe state; they own none of it. A response model is a
projection of rows the command read, built per request and discarded. The one
artifact this page's material persists into is `openapi.json`, which is
committed and regenerated, never edited.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `test_committed_openapi_json_matches_the_live_app_surface` fails | A model or route changed without regenerating. | Run both regeneration scripts and commit the artifacts. |
| A new command's result is `unknown` in TypeScript | No response model registered for it. | Add a model and put it in that module's `RESPONSE_MODELS`; `tests/test_response_model_registry.py` is the guard. |
| `500` with a pydantic `ValidationError` in the daemon log | A response model is stricter than the data it projects — an `int` field receiving a rolled-up average, for example. | Widen the field to match reality, regenerate, and add a case with the real shape. |
| A field you added is missing from the client | The client was not regenerated, or the installed client is an older copy than the checkout. | Regenerate. An editable install then already sees the new files; if the script reports the installed client comes from another tree, run with `PYTHONPATH=packages/aq-client` rather than re-pointing a shared environment. |

## Related pages

* [HTTP API overview](README.md) — where these models are served from.
* [Python client](python-client.md) — how the schemas become typed calls.
* [TypeScript client](typescript-client.md) — the same, for the dashboard.
* [Module catalog: API](../modules/api.md) — one row per model module.

## Source and tests

[`src/api/models/`](../../../src/api/models/),
[`src/api/codegen.py`](../../../src/api/codegen.py).

```bash
aq test tests/test_response_model_registry.py tests/test_api_codegen_input_models.py \
        tests/test_playbook_v2_api_dtos.py tests/test_api_client_contract.py
```
