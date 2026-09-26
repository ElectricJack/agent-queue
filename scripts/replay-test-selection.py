#!/usr/bin/env python3
"""Evaluate historical red commits without running tests or promoting a policy.

Cases are a JSON list of ReplayCase objects. Optional existing_worker_selection
is a list of module paths for comparison. Baseline known_failures must be exact
node IDs from contemporaneous evidence; artifact_expired/ambiguous_cause exclude
untrustworthy labels. Only --with-jev enables provider requests (daemon-side key
in TYPESAFE_API_KEY); the default is offline and cannot earn Jev promotion.
Runtime savings must be evaluated separately before any operator promotion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_cases(path):
    from src.test_selection.replay import ReplayCase

    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("cases must be a JSON list")
    cases = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"case {index}: expected object")
        values = dict(row)
        for name in ("commit", "base", "source"):
            if not isinstance(values.get(name), str) or not values[name]:
                raise ValueError(f"case {index}: {name} must be a non-empty string")
        if values["source"] not in ("ci", "worker"):
            raise ValueError(f"case {index}: source must be ci or worker")
        timestamp = values.get("observed_at")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (float, int)):
            raise ValueError(f"case {index}: observed_at must be a finite epoch")
        if not math.isfinite(timestamp):
            raise ValueError(f"case {index}: observed_at must be a finite epoch")
        for name in ("artifact_expired", "ambiguous_cause"):
            if name in values and not isinstance(values[name], bool):
                raise ValueError(f"case {index}: {name} must be boolean")
        for name in (
            "failing_modules",
            "failing_node_ids",
            "known_failures",
            "existing_worker_selection",
        ):
            if name not in values and name in ("known_failures", "existing_worker_selection"):
                continue
            items = values.get(name)
            if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                raise ValueError(f"case {index}: {name} must be a string list")
            values[name] = frozenset(items)
        worker = values.pop("existing_worker_selection", None)
        cases.append((ReplayCase(**values), worker))
    return cases


async def _replay(args, cases):
    from src.config import TestSelectionConfig
    from src.git.manager import GitManager
    from src.test_selection.replay import replay_case, score
    from src.test_selection.service import SelectionService
    from src.test_selection.static_impact import PytestImpactedAdapter
    from src.test_selection.typesafe import SdkTransport

    git = GitManager()
    service = None
    if args.with_jev:
        config = TestSelectionConfig(enabled=True, jev_enabled=True)
        key = os.environ.get(config.api_key_env)
        if not key:
            raise ValueError(f"--with-jev requires {config.api_key_env} in the environment")
        transport = SdkTransport(api_key=key, base_url=config.base_url)

        async def default_branch(_project_id):
            return "main"  # Every replay pins an explicit base SHA.

        service = SelectionService(
            db=None,
            git=git,
            config_getter=lambda: config,
            static=PytestImpactedAdapter(),
            transport_factory=lambda _config: transport,
            default_branch_getter=default_branch,
        )
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="aq-selection-replay-") as directory:
        for case, worker in cases:
            outcomes.append(
                await replay_case(
                    git,
                    str(args.repo.resolve()),
                    case,
                    service=service,
                    workdir=Path(directory),
                    existing_worker_selection=worker,
                )
            )
    return score(outcomes, split_at=args.split_at)


def main() -> int:
    from src.git.manager import GitError
    from src.test_selection.catalogue import CatalogueError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument(
        "--split-at", type=float, required=True, help="Held-out starts at this epoch."
    )
    parser.add_argument(
        "--with-jev", action="store_true", help="Explicitly enable live Jev requests."
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if not math.isfinite(args.split_at):
            raise ValueError("split-at must be a finite epoch")
        report = asyncio.run(_replay(args, _load_cases(args.cases)))
        args.out.write_text(report.render_markdown(), encoding="utf-8")
    except (OSError, TypeError, ValueError, GitError, CatalogueError) as exc:
        parser.exit(1, f"replay failed: {exc}\n")
    print(f"Wrote {args.out}: {report.usable}/{report.cases} usable, {report.held_out} held-out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
