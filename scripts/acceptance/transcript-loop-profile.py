#!/usr/bin/env python3
"""Reproduce transcript CPU stalls without a daemon or an operator database.

Run from the checkout: python scripts/acceptance/transcript-loop-profile.py
Output includes a cProfile of one read and health latency including scheduling
delay while a synthetic fleet adopts transcript backlogs sequentially.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import io
import json
from pathlib import Path
import pstats
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.health import router
from src.sessions.transcripts.claude import ClaudeTranscriptReader
from src.sessions.transcripts.codex import CodexTranscriptReader


def fixture(path: Path, harness: str, records: int) -> None:
    if harness == "codex":
        row = {"timestamp": "2026-10-03T21:44:00Z", "type": "response_item",
               "payload": {"type": "function_call_output", "call_id": "tool",
                           "output": "tool output " * 50}}
    else:
        row = {"timestamp": "2026-10-03T21:44:00Z", "type": "assistant", "uuid": "turn",
               "message": {"content": [{"type": "thinking", "thinking": "thought " * 50}],
                           "model": "claude-test", "usage": {"output_tokens": 10}}}
    line = (json.dumps(row) + "\n").encode()
    with path.open("wb") as stream:
        for _ in range(records):
            stream.write(line)


async def measure(path: Path, harness: str, sessions: int) -> dict:
    reader = CodexTranscriptReader() if harness == "codex" else ClaudeTranscriptReader()
    profile = cProfile.Profile()
    profile.enable()
    entries, offset = await reader.read_new(path, 0)
    profile.disable()
    count = len(entries)
    del entries
    output = io.StringIO()
    pstats.Stats(profile, stream=output).sort_stats("tottime").print_stats(12)
    print(f"{harness} reader profile:\n{output.getvalue()}", flush=True)

    app = FastAPI()
    app.include_router(router)
    timings = []
    done = asyncio.Event()
    async with AsyncClient(transport=ASGITransport(app), base_url="http://profile") as client:
        async def probe() -> None:
            while not done.is_set():
                due = time.perf_counter() + 0.01
                await asyncio.sleep(0.01)
                response = await client.get("/health")
                assert response.status_code in (200, 503)
                timings.append(max(0.0, time.perf_counter() - due))

        probe_task = asyncio.create_task(probe())
        await asyncio.sleep(0.02)
        started = time.perf_counter()
        for _ in range(sessions):
            batch, end = await reader.read_new(path, 0)
            assert len(batch) == count and end == offset
            del batch
        elapsed = time.perf_counter() - started
        done.set()
        await probe_task
    timings.sort()
    return {"harness": harness, "sessions": sessions, "bytes_per_session": path.stat().st_size,
            "entries_per_session": count, "elapsed_seconds": round(elapsed, 3),
            "health_probes": len(timings),
            "health_p95_ms": round(timings[int((len(timings) - 1) * 0.95)] * 1000, 3),
            "health_max_ms": round(max(timings) * 1000, 3)}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=8)
    parser.add_argument("--records", type=int, default=100_000)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="aq-transcript-profile-") as folder:
        results = []
        for harness in ("codex", "claude"):
            path = Path(folder) / f"{harness}.jsonl"
            fixture(path, harness, args.records)
            results.append(await measure(path, harness, args.sessions))
        print(json.dumps(results, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
