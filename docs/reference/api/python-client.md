# Python client

`agent-queue-api-client` is a typed Python client for the whole
[HTTP API](README.md). It is **generated** from
[`openapi.json`](../../../openapi.json) and committed to the repository under
[`packages/aq-client/`](../../../packages/aq-client/) — 1,439 tracked files,
none of them hand-written.

Use it when you are writing Python against a running daemon and want the
request and response types checked. For one-off shell work, `aq` and `curl`
are lighter.

> **Note.** The `aq` CLI does **not** use this package as its transport. It
> posts to `/api/execute` ([`src/cli/client.py`](../../../src/cli/client.py)),
> which is deliberate: the generated client is a consumer of the API, not the
> CLI's plumbing. The typed-dispatch machinery in that module is kept working
> by [`tests/test_cli_client_generated.py`](../../../tests/test_cli_client_generated.py)
> but has no live caller.

## Install

```bash
pip install -e packages/aq-client
```

That installs the checkout's copy. Reinstall after every regeneration, or the
installed package and the daemon you are calling drift apart.

## Quickstart

> Assumes a running daemon and a task you can read. Inside an agent session,
> `AQ_API_URL` and `AQ_API_TOKEN` are already set; as a local operator, use
> `Client` with no token and the daemon treats you as the trusted local
> identity.

```python
import os

from agent_queue_api_client import AuthenticatedClient
from agent_queue_api_client.api.task import task_show
from agent_queue_api_client.models import TaskShowRequest

client = AuthenticatedClient(
    base_url=os.environ["AQ_API_URL"], token=os.environ["AQ_API_TOKEN"]
)
with client as c:
    task = task_show.sync(client=c, body=TaskShowRequest(task_id="solid-grove.13"))

print(type(task).__name__)
print(task.id, task.status, task.title)
```

```text
TaskShowResponse
solid-grove.13 IN_PROGRESS Document REST, WebSocket and generated Python/TypeScript clients
```

Creating a task is the same shape — one module per operation, one request
model, one response model:

```python
from agent_queue_api_client.api.task import create_task
from agent_queue_api_client.models import CreateTaskRequest

created = await create_task.asyncio(
    client=c,
    body=CreateTaskRequest(
        project_id="demo",
        title="Document the API",
        description="Write docs/reference/api.",
    ),
)
print(created.task_id, created.status)
```

```text
clear-summit READY
```

### Four ways to call every operation

| Function | Returns |
|---|---|
| `sync(client=…, body=…)` | The parsed model, or `None` for an undeclared status. |
| `sync_detailed(…)` | `Response[…]` with `status_code`, `content`, `headers` and `parsed`. |
| `asyncio(…)` | The parsed model, awaited. |
| `asyncio_detailed(…)` | The detailed response, awaited. |

A documented failure is *parsed*, not raised: `task_show.sync` returns a
`TaskShowResponse422` when the task does not exist. Use the `_detailed`
variants when you need the status code, and set
`raise_on_unexpected_status=True` on the client if you would rather an
undeclared status raise `errors.UnexpectedStatus` than return `None`.

`Client` sends no `Authorization` header — the trusted local identity.
`AuthenticatedClient` sends `Authorization: Bearer <token>`, which the daemon
resolves to a session scope; what such a token may call is on the
[conventions](conventions.md#what-a-session-token-may-call) page.

## Layout

```text
packages/aq-client/agent_queue_api_client/
  client.py     Client / AuthenticatedClient, httpx wiring
  errors.py     UnexpectedStatus
  types.py      Response[…], UNSET
  api/<group>/  one module per operation
  models/       one module per schema (1,121 of them)
```

`api/` mirrors the API's categories exactly, so the group tells you the path:

| Group | Operations | Group | Operations |
|---|---|---|---|
| `task` | 51 | `mcp` | 7 |
| `system` | 39 | `message` | 7 |
| `default` | 33 | `notes` | 7 |
| `project` | 28 | `escalation` | 6 |
| `playbook` | 27 | `pool` | 4 |
| `agent` | 23 | `formula` | 3 |
| `git` | 21 | `sessions` | 3 |
| `files` | 11 | `digest` | 2 |
| `plugin` | 11 | `graph` | 2 |
| | | `memory` | 2 |
| | | `discord` | 1 |

`default` holds the operations with no category — the hand-written resource
routes listed in the [overview](README.md#2-hand-written-resource-and-stream-routes).
`sessions` holds the three message-relay routes. Regenerate the counts with:

```bash
for d in packages/aq-client/agent_queue_api_client/api/*/; do
  echo "$(basename "$d") $(ls "$d"/*.py | grep -vc __init__)"
done
```

The generated tree covers only what OpenAPI can describe. There is no client
for `/ws/events` or the SSE endpoints — see [streaming](events.md).

## Regeneration and version compatibility

```bash
./scripts/regenerate-api-client.sh --offline    # rebuild openapi.json, then the client
./scripts/regenerate-api-client.sh --from-file  # reuse the committed openapi.json
./scripts/regenerate-api-client.sh              # fetch the spec from a running daemon
```

`--offline` is the canonical path: `create_app()` builds the whole route
surface from the command registry, so the spec is a pure function of the
checkout and needs no daemon.

The tree is a function of **three** pinned inputs, and all three are checked:

| Input | Pin | Guard |
|---|---|---|
| The spec | `openapi.json`, committed | `test_committed_openapi_json_matches_the_live_app_surface` |
| The generator | `openapi-python-client==0.29.0` — `GENERATOR_VERSION` in the script and the `dev` extra in [`pyproject.toml`](../../../pyproject.toml) | `test_generator_version_pin_agrees_between_the_script_and_the_dev_extra`, and the script refuses to run with another version |
| The post hooks | `ruff check --fix-only` / `ruff format`, scoped to the Python package in [`scripts/openapi-python-client.yaml`](../../../scripts/openapi-python-client.yaml) | `test_generated_client_readme_does_not_depend_on_the_ambient_formatter` |

The hook scoping is not cosmetic. The generator's default hooks run `ruff` at
the project root, and a recent `ruff` reformats the Python code blocks *inside*
the generated `README.md` — so the committed README became a function of
whichever box last regenerated it, and CI failed deterministically on `main`.
Scoping both hooks to `agent_queue_api_client/` leaves the README exactly as
the generator's template writes it.

Because the boilerplate check needs the pinned generator installed and skips
where it is not, the script also records digests of the generator-only files in
[`scripts/aq-client-boilerplate.sha256`](../../../scripts/aq-client-boilerplate.sha256),
and `test_generated_client_boilerplate_matches_the_recorded_digests` verifies
them from the checkout alone.

**Never hand-edit a file under `packages/aq-client/`.** Regenerate it. A
hand-edited `README.md` once landed with both the pin and the tree check
already in place, which is why the digest file exists.

### Which daemon does this client match?

The package version (`0.1.0`) is not a compatibility signal — it comes from
the spec's `info.version` and does not move. The real answer is: the client in
your checkout matches the daemon built from that same checkout. Two ways to
check a mismatch:

```bash
# Does this checkout's committed spec still describe this checkout's app?
aq test tests/test_api_client_contract.py

# Does the running daemon serve the same operations?
diff <(python3 -c "import json;print('\n'.join(sorted(o['operationId'] \
  for p in json.load(open('openapi.json'))['paths'].values() for o in p.values())))") \
     <(curl -s http://127.0.0.1:8081/openapi.json | python3 -c "import json,sys; \
  print('\n'.join(sorted(o['operationId'] for p in json.load(sys.stdin)['paths'].values() for o in p.values())))")
```

An empty diff means the running daemon and your client agree on the operation
set. The daemon on a box often runs a different branch from your worktree, so
this is worth checking before blaming the client.

## State ownership

The client is stateless: one `httpx` client, no cache, no local storage. Every
answer comes from the daemon. `packages/aq-client/` itself is a committed
artifact owned by the regeneration script.

## Common failures and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `ModuleNotFoundError: agent_queue_api_client` | Not installed. | `pip install -e packages/aq-client`. |
| The installed client is not the checkout's | An editable install points at another tree. | `python3 -c "import agent_queue_api_client as m; print(m.__file__)"`, then reinstall from this checkout, or run with `PYTHONPATH=packages/aq-client`. |
| A call returns `None` | The daemon answered a status the operation does not declare. | Use `sync_detailed` to see `status_code` and `content`, or set `raise_on_unexpected_status=True`. |
| `Response422` instead of your model | The command ran and returned an error — this is the documented failure path, not a client bug. | Read `.error` on the parsed object. |
| `403 out of scope: …` | A session token called something outside the agent surface. | See [conventions](conventions.md#what-a-session-token-may-call). |
| `git diff` is dirty right after regenerating | Wrong generator version, or `ruff` missing. | Install `openapi-python-client==0.29.0` and `ruff` (`pip install -e '.[dev]'`), then regenerate. |

## Related pages

* [HTTP API overview](README.md) — the surface this client wraps.
* [Response models](models.md) — where the model classes come from.
* [TypeScript client](typescript-client.md) — the same spec, the other client.
* [Streaming](events.md) — what this client deliberately does not cover.

## Source and tests

[`packages/aq-client/`](../../../packages/aq-client/) (generated),
[`scripts/regenerate-api-client.sh`](../../../scripts/regenerate-api-client.sh),
[`src/api/spec.py`](../../../src/api/spec.py).

```bash
aq test tests/test_api_client_contract.py tests/test_cli_client_generated.py
```
