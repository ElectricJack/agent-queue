# CI observation recovery and notification pressure — 2026-10-25

Task: `fresh-glacier-53`.

## Findings

The supervisor's live PostgreSQL inspection found no lock waiters. It found
pool pressure (average checkout wait about 1.4 seconds, maximum about 4.7 seconds)
and a saturated daemon CPU. These observations supersede the original
self-deadlock hypothesis; this change retains the graph fencing contract.

Two independent error paths made a temporary observation timeout persistent:

- `DispatchWaits.sync` let `WaitError` escape when checking whether an existing
  CI subscription could be removed, aborting the train visit.
- `WakeDispatcher.commit` returned success without a committed outcome on
  observation failure. The listener then stamped the source event dispatched.
  `CheckWatcher` suppresses unchanged snapshots, so a green result could remain
  pending indefinitely with no replacement event.

The fix preserves existing subscriptions on observation failure and retains
failed source events with `dispatched_at IS NULL`. The main cursor continues,
while bounded retry passes reobserve those events with fresh fenced evidence.
Provider retry delays are respected in process; a restarted listener discovers
retained events from the existing outbox index. Warnings include source, event
sequence, retry delay and event age; successful retries are logged separately.

Recovery of the already affected live waits belongs to the supervisor after
publication/deployment. Existing events stamped by the old implementation cannot
be distinguished from successful dispatches by the new retry scan.

## Notification profile

Attaching `py-spy` to the daemon failed with host ptrace permission denied. A
local `cProfile` experiment instead used the real event bus, event-gate subscriber,
V2 playbook runtime, SQLAlchemy engine and a disposable PostgreSQL test clone.
It emitted 200 schema-valid `notify.task_message` events, awaiting each runtime
batch. There were no active playbooks or open gates.

| SELECT target | Queries for 200 notifications | Queries per notification |
|---|---:|---:|
| gates | 200 | 1 |
| tasks | 200 | 1 |
| playbook_activations | 200 | 1 |
| projects | 400 | 2 |
| integration_legacy_suppression | 200 | 1 |
| **Total** | **1200** | **6** |

The profiled loop took 3.840 seconds wall time and 2.604 seconds process CPU,
with 1,177,067 Python calls including profiler overhead. The largest internal
costs included epoll waiting (1.000 seconds), socket sends (0.375 seconds), and
greenlet switching (0.212 seconds). These are synthetic measurements, not a
live-daemon attribution or a throughput budget.

The traced path is `TranscriptWatcher` → `notify.task_message` → wildcard
subscribers. `Orchestrator._subscribe_event_gates` looks up open event gates on
every message. `PlaybookEngine.dispatch_event` hydrates the task and reads ready
activations before discovering none match; legacy-review suppression and the
separate development-review suppression check read the project twice.

This establishes avoidable database and Python work even with no consumers.
A follow-up can evaluate filtering subscriptions before hydration and avoiding
duplicate suppression reads, with event-routing coverage. No notification policy
or runtime optimization is included in this containment fix.

## Reproduction

Save the following as a temporary Python script and run from the repository with
`PYTHONPATH=.` and `POSTGRES_TEST_DSN` pointing at the disposable test PostgreSQL
service. It clones the test template into a uniquely named database and drops
only that clone afterward. It never initializes or migrates the operator database.
The raw and text profiles are written under `/tmp`.

```python
import asyncio
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
        runtime.subscribe_to_events()
        await Orchestrator._subscribe_event_gates(orch)
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
        profiler.dump_stats('/tmp/fresh-glacier-53-notifications.prof')
        with open('/tmp/fresh-glacier-53-notifications.txt', 'w') as output:
            output.write(json.dumps({'notifications': 200, 'elapsed_seconds': elapsed, 'cpu_seconds': cpu_seconds,
                                     'queries': dict(counts)}, indent=2) + '\n')
            pstats.Stats(profiler, stream=output).strip_dirs().sort_stats('tottime').print_stats(35)
        print(json.dumps({'notifications': 200, 'elapsed_seconds': elapsed, 'cpu_seconds': cpu_seconds,
                          'queries': dict(counts)}))
    finally:
        await db.close()
        await drop_database(BASE, name)

asyncio.run(main())
```
