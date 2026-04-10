"""Test orchestration and result collection for supervisor evaluation.

Can be run as a module: python -m tests.chat_eval.runner --output tests/chat_eval/results/

Updated: ChatAgent → Supervisor (post-supervisor refactor).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from src.supervisor import Supervisor
from src.chat_providers.base import ChatProvider
from src.config import AppConfig
from src.orchestrator import Orchestrator

from tests.chat_eval.metrics import (
    EvalRunResult,
    TestCaseResult,
    TurnResult,
    aggregate_results,
    evaluate_turn,
    save_run,
)
from tests.chat_eval.recording_handler import RecordingCommandHandler
from tests.chat_eval.report import generate_report
from tests.chat_eval.test_cases._loader import load_all_cases
from tests.chat_eval.test_cases._types import TestCase


class _VerboseProviderWrapper:
    """Wraps a ChatProvider to print LLM call progress without monkey-patching."""

    def __init__(self, inner: ChatProvider):
        self._inner = inner
        self.call_count = 0
        self._start: float = 0.0

    def reset(self, start: float) -> None:
        self.call_count = 0
        self._start = start

    async def create_message(self, *a, **kw):
        self.call_count += 1
        if self.call_count > 1:
            elapsed = time.monotonic() - self._start
            print(f" call {self.call_count} ({elapsed:.0f}s) ...", end="", flush=True)
        return await self._inner.create_message(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def run_single_case(
    orchestrator: Orchestrator,
    config: AppConfig,
    provider: ChatProvider,
    case: TestCase,
    verbose: bool = False,
    **_kwargs,
) -> TestCaseResult:
    """Execute a single test case and evaluate results.

    Each case gets a fresh Supervisor to prevent cross-contamination.
    """
    # Fresh agent per case — use a wrapper for verbose logging so we never
    # monkey-patch the shared provider object.
    wrapper = _VerboseProviderWrapper(provider) if verbose else None
    agent = Supervisor(orchestrator, config)
    agent._provider = wrapper if wrapper else provider

    recorder = RecordingCommandHandler(orchestrator, config)
    agent.handler = recorder

    case_result = TestCaseResult(
        case_id=case.id,
        category=case.category,
        difficulty=case.difficulty.value,
        tags=case.tags,
    )

    try:
        # Set case-level active project
        if case.active_project:
            agent.set_active_project(case.active_project)

        # Run setup commands
        for cmd_name, cmd_args in case.setup_commands:
            await recorder.execute(cmd_name, cmd_args)
        recorder.reset()  # Don't count setup calls

        # Run each turn
        history: list[dict] = []
        for ti, turn in enumerate(case.turns, 1):
            # Override active project per-turn if specified
            if turn.active_project is not None:
                agent.set_active_project(turn.active_project)

            recorder.reset()
            start = time.monotonic()

            if verbose:
                turn_label = f"turn {ti}/{len(case.turns)} " if len(case.turns) > 1 else ""
                print(f"          {turn_label}LLM call 1 ...", end="", flush=True)
                wrapper.reset(start)

            response = await agent.chat(
                turn.user_message,
                user_name="test_user",
                history=history,
            )

            elapsed = time.monotonic() - start

            if verbose:
                tools_used = [c.name for c in recorder.calls] if recorder.calls else ["(none)"]
                print(f" done ({elapsed:.1f}s, tools: {', '.join(tools_used)})", flush=True)

            # Evaluate this turn
            turn_result = evaluate_turn(turn, recorder.calls)
            turn_result.latency = elapsed
            case_result.turn_results.append(turn_result)

            # Build history for multi-turn
            history.append({"role": "user", "content": turn.user_message})
            history.append({"role": "assistant", "content": response})

        # Case passes if all turns pass
        case_result.passed = all(tr.passed for tr in case_result.turn_results)

    except Exception as e:
        if verbose:
            print(f" ERROR: {e}", flush=True)
        case_result.error = str(e)
        case_result.passed = False

    return case_result


async def run_eval(
    orchestrator: Orchestrator,
    config: AppConfig,
    cases: list[TestCase],
    provider: ChatProvider,
    model_name: str = "",
    verbose: bool = False,
    output_dir: str = "",
    concurrency: int = 1,
    **_kwargs,
) -> EvalRunResult:
    """Run all cases and return aggregated results.

    When *output_dir* is set, intermediate results are saved after every case
    so that progress is preserved if the run is interrupted.

    *concurrency* controls how many cases run in parallel.  With a local
    Ollama model, 2-3 can help overlap Python/network overhead even though
    GPU inference is mostly sequential.
    """
    from pathlib import Path

    case_results: list[TestCaseResult] = []
    total = len(cases)
    passed = 0
    completed = 0
    eval_start = time.monotonic()
    model = model_name or provider.model_name
    print_lock = asyncio.Lock()

    # Prepare incremental output path
    partial_path: Path | None = None
    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        partial_path = out / f"eval_{model}_{int(time.time())}_partial.json"

    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(i: int, case: TestCase) -> TestCaseResult:
        nonlocal passed, completed
        async with semaphore:
            n_turns = len(case.turns)
            msg_preview = case.turns[0].user_message[:60] if case.turns else ""
            async with print_lock:
                print(
                    f"  [{i}/{total}] {case.id} ({n_turns} "
                    f"turn{'s' if n_turns != 1 else ''}) "
                    f'— "{msg_preview}" ...',
                    flush=True,
                )

            case_start = time.monotonic()
            result = await run_single_case(
                orchestrator,
                config,
                provider,
                case,
                verbose=verbose,
            )
            case_elapsed = time.monotonic() - case_start

            async with print_lock:
                completed += 1
                passed += result.passed
                status = "PASS" if result.passed else "FAIL"
                rate = passed / completed * 100
                elapsed_total = time.monotonic() - eval_start
                print(
                    f"          {status}  {case_elapsed:.1f}s  "
                    f"({rate:.0f}% passing, {completed}/{total} done, "
                    f"{elapsed_total:.0f}s elapsed)",
                    flush=True,
                )

            return result

    async def _save_partial():
        if partial_path and case_results:
            partial = aggregate_results(list(case_results), model=model)
            save_run(partial, partial_path.parent, filename=partial_path.name)

    if concurrency <= 1:
        # Sequential path — preserves order and incremental saves
        for i, case in enumerate(cases, 1):
            result = await _run_one(i, case)
            case_results.append(result)
            await _save_partial()
    else:
        # Concurrent path — save incrementally as each case completes
        async def _run_and_collect(i: int, case: TestCase) -> None:
            result = await _run_one(i, case)
            async with print_lock:
                case_results.append(result)
                await _save_partial()

        tasks = [_run_and_collect(i, case) for i, case in enumerate(cases, 1)]
        await asyncio.gather(*tasks)

    run_result = aggregate_results(case_results, model=model)

    # Clean up partial file now that we have the final result
    if partial_path and partial_path.exists():
        partial_path.unlink()

    return run_result


async def _main(args: argparse.Namespace) -> None:
    """CLI entry point for running evaluations."""
    import tempfile
    from pathlib import Path
    from src.chat_providers import create_chat_provider
    from src.config import ChatProviderConfig, load_config

    # Load real config for chat_provider defaults, fall back gracefully
    config_path = os.path.expanduser("~/.agent-queue/config.yaml")
    try:
        base_config = load_config(config_path)
        chat_cfg = base_config.chat_provider
    except (FileNotFoundError, Exception):
        chat_cfg = ChatProviderConfig()

    # CLI args override config.yaml values
    provider_name = args.provider or chat_cfg.provider
    model_name = args.model or chat_cfg.model

    # Create temp dir for DB
    with tempfile.TemporaryDirectory() as tmp:
        config = AppConfig(
            database_path=str(Path(tmp) / "eval.db"),
            workspace_dir=str(Path(tmp) / "workspaces"),
            data_dir=str(Path(tmp) / "data"),
            chat_provider=ChatProviderConfig(
                provider=provider_name,
                model=model_name,
                base_url=chat_cfg.base_url,
                keep_alive=chat_cfg.keep_alive,
            ),
        )

        # Import MockAdapterFactory locally to avoid circular deps at top level
        from tests.chat_eval.conftest import MockAdapterFactory

        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        try:
            provider = create_chat_provider(config.chat_provider)
            if not provider:
                print("Failed to create chat provider. Check API key.", file=sys.stderr)
                sys.exit(1)

            cases = load_all_cases()
            if args.category:
                cases = [c for c in cases if c.category == args.category]
            if args.difficulty:
                cases = [c for c in cases if c.difficulty.value == args.difficulty]

            if args.limit:
                cases = cases[: args.limit]

            print(
                f"Running {len(cases)} test cases with {provider.model_name} "
                f"(concurrency={args.concurrency})..."
            )
            run_result = await run_eval(
                orch,
                config,
                cases,
                provider,
                verbose=args.verbose,
                output_dir=args.output,
                concurrency=args.concurrency,
            )

            # Save results
            output_dir = Path(args.output)
            json_path = save_run(run_result, output_dir)
            print(f"Results saved to {json_path}")

            # Generate report
            report = generate_report(run_result)
            report_path = output_dir / f"report_{int(run_result.timestamp)}.md"
            report_path.write_text(report)
            print(f"Report saved to {report_path}")

            print(
                f"\nPass rate: {run_result.pass_rate:.1%} "
                f"({run_result.passed_cases}/{run_result.total_cases})"
            )

        finally:
            await orch.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Run chat agent evaluation")
    parser.add_argument(
        "--output", default="tests/chat_eval/results/", help="Output directory for results"
    )
    parser.add_argument(
        "--provider",
        default="",
        help="Chat provider (anthropic, ollama); default: from config.yaml",
    )
    parser.add_argument(
        "--model", default="", help="Model name override; default: from config.yaml"
    )
    parser.add_argument("--category", default="", help="Filter by category")
    parser.add_argument("--difficulty", default="", help="Filter by difficulty")
    parser.add_argument(
        "--limit", "-n", type=int, default=0, help="Run only the first N cases (0 = all)"
    )
    parser.add_argument(
        "--concurrency", "-j", type=int, default=1, help="Run N cases in parallel (default: 1)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show per-turn LLM call progress"
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
