# Notification dispatch CPU and database reads — 2026-10-25

Task: `clear-falcon-33`, following `fresh-glacier-53`.

## Change

`V2PlaybookRuntime.refresh()` already loads every ready artifact's trigger names.
The bus callback now rejects event types absent from this catalog before copying
payloads, creating background tasks, hydrating task rows or querying activations
and legacy review suppression. Scope, literal filters and guards remain engine
responsibilities. Activation commands publish a fresh catalog after committing.
Before refresh, on refresh failure, or with an unloadable ready artifact, the
runtime conservatively dispatches events. Refresh generations prevent an older
query from publishing stale negative coverage after a newer refresh is cancelled.
Required routing retention still receives
`task.route_needed`, even with an empty catalog. Durable integration delivery is
unchanged.

The event-gate listener now remembers which event types have open gates for up to
five seconds. Unmatched types skip database reads during that interval. Event-gate
mutation notifications invalidate the cache immediately, and a generation fence
prevents an older query from overwriting concurrent invalidation. Matching events
still read open gates before resolving them. Query failures do not establish empty
interest. Cache expiry and the persisted-event sweep recover missed notifications.

## Local cProfile comparison

The production daemon cannot be attached with py-spy under this host's ptrace
policy. This experiment uses the real event bus, gate subscriber, V2 runtime,
SQLAlchemy engine and a disposable PostgreSQL clone. It isolates the identified
notification paths, rather than starting or modifying the operator daemon.

Both runs emit 200 schema-valid `notify.task_message` events sequentially, awaiting
any background dispatch after each emit. There are no enabled playbooks or open
gates, matching the earlier experiment. Runtime catalog refresh and setup are
outside the counters and timing. The independent integration-pending reconciler
is disabled in both runs so its periodic queries do not contaminate this sample.
The baseline restores the original gate callback from `d7308c571` in the benchmark
process only and disables the new runtime prefilter; the worktree is not reverted.

| Measurement | Before | After |
|---|---:|---:|
| Notifications | 200 | 200 |
| Process CPU seconds (cProfile enabled) | 2.889542 | 0.016325 |
| Wall seconds | 4.438864 | 0.028374 |
| Gate SELECTs | 200 | 1 |
| Task SELECTs | 200 | 0 |
| Activation SELECTs | 200 | 0 |
| Project SELECTs | 400 | 0 |
| Legacy suppression SELECTs | 200 | 0 |
| Total SELECTs | 1,200 | 1 |

This sample reduces notification-path CPU by about 99.4% and database reads from
six per event to one per cache interval (the sample fits within one interval).
These are synthetic, profiled process measurements on a shared host, not a
live-daemon CPU percentage or a wall-clock performance guarantee. Verifying the
reported 95% live CPU condition and train-visit latency requires deployment and
operator observation; this worker does not restart the production daemon.

Raw measurements from this run are `/tmp/clear-falcon-33-before.prof`,
`/tmp/clear-falcon-33-after.prof` and the corresponding `.txt` summaries.

## Validation

- `aq test tests/test_playbook_pending_events_policy.py tests/test_work_graph_cascade.py -q`:
  91 passed.
- `aq test tests/test_v2_engine.py tests/test_required_playbooks.py tests/test_routing_admission_v2.py tests/test_playbook_activation_commands.py tests/test_integration_outbox.py tests/test_gate_queries.py tests/test_event_bus.py tests/test_transcript_watcher.py -q`:
  429 passed.
- After adding the cancelled-refresh generation fence,
  `aq test tests/test_playbook_pending_events_policy.py tests/test_routing_admission_v2.py -q`:
  90 passed, including the new concurrent refresh regression.
- `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh`: all 21 scenarios passed
  using isolated home `/tmp/aq-e2e-clear-falcon-33`, API port `18115`, fake session
  provider and database `aq_e2e_clear_falcon_33` on the disposable test server
  (`localhost:5534`). The wrapper stopped its daemon afterward.
- Ruff on changed Python files and `git diff --check`: passed.

## Reproduction

Save the script below to a temporary file and run each mode in a fresh process
from the repository. Set `POSTGRES_TEST_DSN` to the disposable test PostgreSQL
service and keep the worker's production-database refusal variables unchanged:

```bash
PYTHONPATH=. python /tmp/profile-notifications.py before
PYTHONPATH=. python /tmp/profile-notifications.py after
```

The script clones the test template and drops only its unique clone afterward.

```python
import asyncio
import ast
import subprocess
import sys
import cProfile
import json
import os
import pstats
import time
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import event
from src.commands import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.database.engine import create_postgres_engine
from src.event_bus import EventBus
from src.models import Project, Task
from src.orchestrator.core import Orchestrator
from src.playbooks.runtime import V2PlaybookRuntime
from tests.db_fixtures import clone_database, drop_database

BASE = os.environ['POSTGRES_TEST_DSN']
MODE = sys.argv[1]
if MODE == 'before':
    source = subprocess.check_output(['git', 'show', 'd7308c5712a61589a04f6c33f507cd7bbb5cf9ea:src/orchestrator/core.py'], text=True)
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Orchestrator')
    method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_subscribe_event_gates')
    import src.orchestrator.core as core
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<baseline-gate-handler>', 'exec'), core.__dict__)
    Orchestrator._subscribe_event_gates = core._subscribe_event_gates

async def main():
    name = 'aq_notification_profile_' + uuid.uuid4().hex[:12]
    dsn = await clone_database(BASE, name)
    db = Database(dsn)
    db._engine = create_postgres_engine(dsn)
    try:
        await db.create_project(Project(id='p', name='Profile'))
        await db.create_task(Task(id='t', project_id='p', title='Profile', description=''))
        counts = Counter()
        def count(conn, cursor, statement, parameters, context, executemany):
            words = statement.split()
            counts[words[words.index('FROM') + 1] if 'FROM' in words else words[0]] += 1
        event.listen(db._engine.sync_engine, 'before_cursor_execute', count)
        config, bus = AppConfig(), EventBus()
        orch = SimpleNamespace(db=db, config=config, bus=bus, plugin_registry=None,
                               _gate_event_unsub=None)
        handler = CommandHandler(orch, config)
        runtime = V2PlaybookRuntime(config=config, db=db, handler=handler, llm=None, bus=bus)
        runtime._ensure_integration_reconciler = lambda: None
        await runtime.refresh()
        if MODE == "before":
            runtime._event_triggers = None
        runtime.subscribe_to_events()
        await Orchestrator._subscribe_event_gates(orch)
        counts.clear()
        profiler = cProfile.Profile()
        start = time.perf_counter()
        cpu_start = time.process_time()
        profiler.enable()
        for n in range(200):
            await bus.emit('notify.task_message', {'project_id': 'p', 'task_id': 't',
                'message': 'benchmark', 'event_type': 'notify.task_message',
                'severity': 'info', 'category': 'task_stream'})
            if runtime._tasks:
                await asyncio.gather(*runtime._tasks)
        profiler.disable()
        elapsed = time.perf_counter() - start
        cpu_seconds = time.process_time() - cpu_start
        profiler.dump_stats(f'/tmp/clear-falcon-33-{MODE}.prof')
        with open(f'/tmp/clear-falcon-33-{MODE}.txt', 'w') as output:
            output.write(json.dumps({'notifications': 200, 'elapsed_seconds': elapsed, 'cpu_seconds': cpu_seconds,
                                     'queries': dict(counts)}, indent=2) + '\n')
            pstats.Stats(profiler, stream=output).strip_dirs().sort_stats('tottime').print_stats(35)
        print(json.dumps({'notifications': 200, 'elapsed_seconds': elapsed, 'cpu_seconds': cpu_seconds,
                          'queries': dict(counts)}))
    finally:
        await runtime.shutdown()
        await db.close()
        await drop_database(BASE, name)

asyncio.run(main())
```
