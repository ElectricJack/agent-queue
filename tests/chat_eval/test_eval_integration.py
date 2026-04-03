"""LLM evaluation tests (on-demand, require API key).

Parametrized over all test cases. Uses real ChatProvider from env vars.
Run with: ANTHROPIC_API_KEY=... pytest tests/chat_eval/test_eval_integration.py -m eval -v

Updated: ChatAgent → Supervisor (post-supervisor refactor).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.supervisor import Supervisor
from src.chat_providers import create_chat_provider
from src.config import AppConfig, ChatProviderConfig
from src.orchestrator import Orchestrator

from tests.chat_eval.metrics import evaluate_turn
from tests.chat_eval.providers import RecordingProvider
from tests.chat_eval.recording_handler import RecordingCommandHandler
from tests.chat_eval.test_cases._loader import load_all_cases, case_ids
from tests.chat_eval.test_cases._types import TestCase


def _has_eval_credentials() -> bool:
    """Check if any valid credentials are available for eval tests.

    Checks (in priority order):
    0. anthropic SDK must be installed
    1. ANTHROPIC_API_KEY env var
    2. EVAL_PROVIDER env var (indicates a provider is explicitly configured)
    3. Claude Code OAuth credentials (~/.claude/.credentials.json)
    """
    try:
        import anthropic  # noqa: F401
    except ModuleNotFoundError:
        return False
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if os.environ.get("EVAL_PROVIDER"):
        return True
    # Check for Claude Code OAuth credentials
    for name in (".credentials.json", "credentials.json"):
        cred_path = Path.home() / ".claude" / name
        if cred_path.exists():
            try:
                creds = json.loads(cred_path.read_text())
                if creds.get("claudeAiOauth", {}).get("accessToken"):
                    return True
            except Exception:
                pass
    return False


# Skip entire module if no credentials available
pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not _has_eval_credentials(),
        reason="No API key or OAuth credentials configured for eval tests",
    ),
]

ALL_CASES = load_all_cases()


@pytest.fixture(scope="module")
def eval_provider_config():
    provider = os.environ.get("EVAL_PROVIDER", "anthropic")
    model = os.environ.get("EVAL_MODEL", "")
    return ChatProviderConfig(provider=provider, model=model)


@pytest.fixture
async def eval_setup(tmp_path, eval_provider_config):
    """Set up orchestrator, provider, and fresh supervisor per test."""
    from tests.chat_eval.conftest import MockAdapterFactory

    config = AppConfig(
        database_path=str(tmp_path / "eval.db"),
        workspace_dir=str(tmp_path / "workspaces"),
        chat_provider=eval_provider_config,
    )

    orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
    await orch.initialize()

    inner_provider = create_chat_provider(config.chat_provider)
    assert inner_provider is not None, "Failed to create provider — check API key"

    recording_provider = RecordingProvider(inner_provider)

    yield orch, config, recording_provider

    await orch.shutdown()


@pytest.mark.parametrize("case", ALL_CASES, ids=case_ids(ALL_CASES))
async def test_eval_case(eval_setup, case: TestCase):
    """Run a single eval case against a real LLM."""
    orch, config, provider = eval_setup

    agent = Supervisor(orch, config)
    agent._provider = provider

    recorder = RecordingCommandHandler(orch, config)
    agent.handler = recorder

    if case.active_project:
        agent.set_active_project(case.active_project)

    # Run setup commands
    for cmd_name, cmd_args in case.setup_commands:
        await recorder.execute(cmd_name, cmd_args)
    recorder.reset()

    # Run turns
    history: list[dict] = []
    all_passed = True

    for turn in case.turns:
        if turn.active_project is not None:
            agent.set_active_project(turn.active_project)

        recorder.reset()
        provider.reset()

        response = await agent.chat(turn.user_message, user_name="eval_user", history=history)

        turn_result = evaluate_turn(turn, recorder.calls)

        if not turn_result.passed:
            all_passed = False
            # Build detailed failure message
            failures = []
            for m in turn_result.tool_matches:
                if not m.matched:
                    failures.append(f"MISSING tool: {m.expected_name}")
                elif not m.args_matched:
                    failures.append(
                        f"ARG MISMATCH for {m.expected_name}: "
                        f"expected {m.expected_args}, got {m.actual_args}, "
                        f"mismatched keys: {m.mismatched_keys}"
                    )
            for f in turn_result.forbidden_tools_called:
                failures.append(f"FORBIDDEN tool called: {f}")
            for u in turn_result.unexpected_tools:
                failures.append(f"UNEXPECTED tool: {u}")

            pytest.fail(
                f'Turn failed: "{turn.user_message}"\n'
                f"Actual tools: {recorder.tool_names_called}\n"
                f"Failures: {failures}"
            )

        history.append({"role": "user", "content": turn.user_message})
        history.append({"role": "assistant", "content": response})

    assert all_passed, f"Case {case.id} had failing turns"
