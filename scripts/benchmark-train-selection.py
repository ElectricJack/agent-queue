#!/usr/bin/env python3
"""Bounded workload model of selection scans and train admission.

Run: python scripts/benchmark-train-selection.py --targets 55 --members 4
Uses real candidate selection/admission with synthetic DB rows and delayed proof
I/O. No daemon, database, network or Git writes. Timings are model measurements,
not attribution of an operator's historical visit. For real Git operation counts
also run scripts/benchmark-git-truth.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.integration import train_sources  # noqa: E402
from src.integration.selection_metrics import (  # noqa: E402
    SelectionMetrics, selection_count, selection_metrics_scope, selection_stage,
)
from src.integration.train import IntegrationTrain, TrainTarget, TrainVisit  # noqa: E402
from src.messages.delivery import MessageDeliveryEngine  # noqa: E402
from src.models import Message  # noqa: E402


@asynccontextmanager
async def connection():
    yield None  # The member window stays below the limit; mocked reads need no SQL.


async def scans(targets, members, proof_delay):
    ids = [f"member-{i}" for i in range(len(targets) * members)]
    routes = {tid: targets[index // members] for index, tid in enumerate(ids)}
    counts = {"scans": 0, "proof_items": 0}

    async def pending(*args, **kwargs):
        counts["scans"] += 1
        return list(ids)

    async def routed(*args, **kwargs):
        return routes

    class Sources(train_sources.DatabaseBatches):
        async def delivered(self, target, snapshot, candidates):
            counts["proof_items"] += len(candidates)
            # One delay per batch models aggregate I/O without creating thousands
            # of Python sleep tasks. Counts, not latency, are the invariant.
            await asyncio.sleep(proof_delay * len(candidates))
            return set()

    source = Sources(SimpleNamespace(_engine=SimpleNamespace(connect=connection)))
    results = []
    with patch.object(train_sources, "_pending_tasks", pending), \
            patch.object(train_sources, "delivery_targets", routed), \
            patch.object(train_sources, "superseded_source_repairs_on", AsyncMock(return_value={})):
        for layout in ("previous_scan_order", "visit_local_window"):
            counts.update(scans=0, proof_items=0)
            metrics = SelectionMetrics()
            started = time.perf_counter()
            with selection_metrics_scope(metrics):
                for target in targets:
                    if layout == "previous_scan_order":
                        for _ in range(2):
                            all_ids = await pending()
                            with selection_stage("root_delivery", items=len(all_ids)):
                                await source.delivered(target, None, all_ids)
                            assert len([tid for tid in all_ids if routes[tid] == target]) == members
                    else:
                        window = await source._candidate_ids(target, None)
                        selection_count("candidate_window_reuses")
                        assert len(window) == members
            results.append({"layout": layout, **counts,
                            "seconds": time.perf_counter() - started,
                            "selection": metrics.as_dict()})
    return results


async def admission(targets, visit_delay):
    fresh = TrainTarget("benchmark", "repo", "refs/heads/new-runnable")
    discovered = list(targets)
    active = peak = inbox_passes = 0
    seen = set()
    clock = [100.0]

    class Train(IntegrationTrain):
        async def visit(self, target):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            try:
                with selection_stage("modeled_proof", items=1):
                    await asyncio.sleep(visit_delay)
                seen.add(target.key)
                return TrainVisit(target, "idle" if target == fresh else "blocked",
                                  target_sha="a" * 40,
                                  detail={"blockers": [{"code": "missing_provenance"}]})
            finally:
                active -= 1

    train = Train(targets=SimpleNamespace(targets=AsyncMock(side_effect=lambda now: discovered)),
                  batches=None, lane_for=None, repair=None, clock=lambda: clock[0])
    db = SimpleNamespace(
        get_pending_recipients=AsyncMock(return_value=[("user", "dashboard", "benchmark")]),
        get_pending_messages=AsyncMock(return_value=[Message(
            "inbox", "benchmark", "system", "model", "user", "dashboard", "probe")]),
        mark_delivered=AsyncMock(return_value=True),
    )
    inbox = MessageDeliveryEngine(db, None, SimpleNamespace(max_inject_per_prompt=1))
    started = time.perf_counter()
    rounds = 0
    try:
        await train.tick()
        await asyncio.sleep(0)
        discovered.append(fresh)
        while fresh.key not in seen:
            assert (await inbox.run_delivery_pass())["delivered"] == 1
            inbox_passes += 1
            await train.drain()
            if fresh.key in seen:
                break
            rounds += 1
            assert rounds <= len(targets) + 1
            await train.tick()
        assert peak <= train.repository_concurrency
        return {"peak_repository_visits": peak, "new_work_rounds": rounds,
                "inbox_passes": inbox_passes, "seconds": time.perf_counter() - started,
                "visited_targets": len(seen)}
    finally:
        await train.stop()


async def main(args):
    targets = [TrainTarget("benchmark", "repo", f"refs/heads/old-{i:03}")
               for i in range(args.targets)]
    print(json.dumps({
        "workload": "synthetic I/O; real selection and admission mechanisms",
        "targets": args.targets, "members_per_target": args.members,
        "scans": await scans(targets, args.members, args.proof_ms / 1000),
        "admission": await admission(targets, args.visit_ms / 1000),
    }, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=int, default=55)
    parser.add_argument("--members", type=int, default=4)
    parser.add_argument("--proof-ms", type=float, default=0.01)
    parser.add_argument("--visit-ms", type=float, default=10)
    args = parser.parse_args()
    if not 1 <= args.targets <= 100 or not 1 <= args.members <= 100:
        parser.error("targets and members must be between 1 and 100")
    if not 0 <= args.proof_ms <= 0.1 or not 0 <= args.visit_ms <= 100:
        parser.error("proof-ms must be 0..0.1 and visit-ms must be 0..100")
    asyncio.run(main(args))
