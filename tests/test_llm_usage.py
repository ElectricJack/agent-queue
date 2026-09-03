"""Provider-reported token accounting for the direct LLM path."""

from __future__ import annotations

from src.config import LLMConfig
from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.llm.types import TokenUsage


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient.with_provider(provider, config=LLMConfig())


def test_usage_addition_is_reported_only_when_both_operands_are_reported() -> None:
    """A missing provider count must prevent a hard-budget caller trusting a sum."""
    total = TokenUsage(1200, 300, reported=True) + TokenUsage(40, 20, reported=False)

    assert total == TokenUsage(1240, 320, reported=False)
    assert total.total == 1560


async def test_complete_propagates_provider_reported_usage() -> None:
    """Removing ``ChatResponse.usage`` would lose the provider's actual counts."""
    provider = FakeProvider()
    provider.add_text('{"risk": "high"}', usage=TokenUsage(1200, 300, reported=True))

    response = await _client(provider).complete("classify")

    assert response.usage == TokenUsage(1200, 300, reported=True)


async def test_run_tools_sums_usage_across_tool_turns() -> None:
    """A later turn must not overwrite the billable usage from an earlier tool turn."""
    provider = FakeProvider()
    provider.add_tool_call("lookup", {"id": "t-1"}, usage=TokenUsage(100, 20, reported=True))
    provider.add_text("done", usage=TokenUsage(60, 10, reported=True))

    async def execute(_name: str, _args: dict) -> dict[str, bool]:
        return {"success": True}

    result = await _client(provider).run_tools(
        "classify",
        [{"name": "lookup", "input_schema": {"type": "object"}}],
        execute,
    )

    assert result.usage == TokenUsage(160, 30, reported=True)


async def test_fake_provider_can_represent_unreported_usage() -> None:
    """The executor needs a deterministic unreporting-provider test double."""
    provider = FakeProvider()
    provider.add_text("done")

    response = await _client(provider).complete("classify")

    assert response.usage is None
