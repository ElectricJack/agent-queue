"""``llm.fallback`` — the direct path's optional second credential.

Provider-failover D13a: while provider availability holds the reserved ``llm``
key unavailable, direct-path calls resolve their class against the fallback's
provider slice and use it; a class with no slice there is ``provider_error``.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest
import yaml

from src.config import (
    AppConfig,
    ConfigValidationError,
    LLMConfig,
    LLMFallbackConfig,
    load_config,
)
from src.intelligence_classes import IntelligenceClass
from src.llm import LLMCallSpec, LLMClient
from src.llm.fake import FakeProvider
from src.llm.providers.errors import ProviderUnavailableError
from src.llm.spec import NoFallbackRoute, resolve_call, resolve_fallback_call
from src.llm.types import ChatResponse, TokenUsage, ToolUseBlock

CLASSES = {
    "fast-low": IntelligenceClass(
        id="fast-low",
        name="Fast · Low",
        description="",
        mapping={
            "anthropic": {"model": "claude-haiku-4-5", "thinking": "low"},
            "openai": {"model": "gpt-6-mini", "reasoning_effort": "low"},
        },
    ),
    "standard-high": IntelligenceClass(
        id="standard-high",
        name="Standard · High",
        description="",
        mapping={
            "anthropic": {"model": "claude-opus-5", "thinking": "xhigh"},
            "openai": {"model": "gpt-6", "reasoning_effort": "xhigh"},
        },
    ),
    # The Astra analogue: one vendor only.
    "anthropic-only": IntelligenceClass(
        id="anthropic-only",
        name="Anthropic only",
        description="",
        mapping={"anthropic": {"model": "claude-fable-5-1"}},
    ),
}

BLOCKED = "provider llm is unauthenticated: 401"


def _config(**fallback: Any) -> LLMConfig:
    fb = {"provider": "openai", "api_key": "sk-fallback", **fallback}
    return LLMConfig(
        provider="anthropic",
        api_key="sk-ant-primary",
        default_class="fast-low",
        fallback=LLMFallbackConfig(**fb),
    )


# -- config ------------------------------------------------------------------


class TestFallbackConfig:
    def test_no_fallback_by_default(self):
        assert LLMConfig().fallback is None
        assert LLMConfig().validate() == []

    def test_a_distinct_credential_validates(self):
        assert _config().validate() == []

    def test_provider_is_required(self):
        errs = LLMConfig(fallback=LLMFallbackConfig()).validate()
        assert [(e.section, e.field) for e in errs] == [("llm", "fallback.provider")]

    def test_unknown_provider_is_rejected(self):
        errs = LLMConfig(fallback=LLMFallbackConfig(provider="bogus")).validate()
        assert any(e.field == "fallback.provider" and "bogus" in e.message for e in errs)

    def test_openai_fallback_needs_an_endpoint_or_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        errs = LLMConfig(fallback=LLMFallbackConfig(provider="openai")).validate()
        assert [e.field for e in errs] == ["fallback.base_url"]
        assert (
            LLMConfig(
                fallback=LLMFallbackConfig(provider="openai", base_url="http://localhost:11434/v1")
            ).validate()
            == []
        )

    def test_the_same_credential_twice_is_rejected(self):
        """Unavailable exactly when the primary is, so it could never serve a call."""
        errs = LLMConfig(
            provider="anthropic",
            api_key="sk-ant-one",
            fallback=LLMFallbackConfig(provider="anthropic", api_key="sk-ant-one"),
        ).validate()
        assert [e.field for e in errs] == ["fallback"]
        # Both empty means both read ANTHROPIC_API_KEY: the same credential too.
        errs = LLMConfig(fallback=LLMFallbackConfig(provider="anthropic")).validate()
        assert [e.field for e in errs] == ["fallback"]

    def test_a_second_key_for_the_same_vendor_is_a_fallback(self):
        cfg = LLMConfig(
            provider="anthropic",
            api_key="sk-ant-one",
            fallback=LLMFallbackConfig(provider="anthropic", api_key="sk-ant-two"),
        )
        assert cfg.validate() == []

    def test_numeric_model_coerced_to_str(self):
        assert LLMFallbackConfig(provider="openai", model=4).model == "4"  # type: ignore[arg-type]

    def test_appconfig_validate_includes_the_fallback(self):
        cfg = AppConfig(llm=LLMConfig(fallback=LLMFallbackConfig(provider="bogus")))
        assert any(e.field == "fallback.provider" for e in cfg.validate())


def _write(tmp_path, llm: Any) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.dump(
            {
                "discord": {"bot_token": "tok", "guild_id": "123"},
                "database": {"url": "postgresql+asyncpg://localhost/aq_test"},
                "llm": llm,
            }
        )
    )
    return str(p)


class TestLoadFallback:
    def test_block_loads(self, tmp_path):
        cfg = load_config(
            _write(
                tmp_path,
                {
                    "provider": "anthropic",
                    "api_key": "sk-ant-primary",
                    "fallback": {
                        "provider": "openai",
                        "api_key": "sk-fallback",
                        "base_url": "https://llm.example/v1",
                        "model": "gpt-6-mini",
                        "default_class": "fast-low",
                    },
                },
            )
        )
        assert cfg.llm.fallback == LLMFallbackConfig(
            provider="openai",
            api_key="sk-fallback",
            base_url="https://llm.example/v1",
            model="gpt-6-mini",
            default_class="fast-low",
        )

    def test_absent_and_null_mean_no_fallback(self, tmp_path):
        assert load_config(_write(tmp_path, {"provider": "anthropic"})).llm.fallback is None
        assert (
            load_config(_write(tmp_path, {"provider": "anthropic", "fallback": None})).llm.fallback
            is None
        )

    def test_legacy_provider_ids_are_normalised(self, tmp_path):
        cfg = load_config(
            _write(tmp_path, {"provider": "anthropic", "fallback": {"provider": "gemini"}})
        )
        assert cfg.llm.fallback.provider == "google"

    def test_a_non_mapping_is_rejected(self, tmp_path):
        with pytest.raises(ConfigValidationError, match=r"\[llm\] fallback: must be a mapping"):
            load_config(_write(tmp_path, {"provider": "anthropic", "fallback": "openai"}))

    def test_an_invalid_block_fails_load(self, tmp_path):
        with pytest.raises(ConfigValidationError, match="fallback.provider"):
            load_config(_write(tmp_path, {"provider": "anthropic", "fallback": {"model": "x"}}))

    def test_unknown_keys_warn_by_name_only(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="src.config"):
            cfg = load_config(
                _write(
                    tmp_path,
                    {
                        "provider": "anthropic",
                        "fallback": {"provider": "google", "apikey": "sk-typo-secret"},
                    },
                )
            )
        assert cfg.llm.fallback.api_key == ""
        warnings = [r.getMessage() for r in caplog.records if "llm.fallback" in r.getMessage()]
        assert warnings and "apikey" in warnings[0]
        assert "sk-typo-secret" not in caplog.text


# -- resolution --------------------------------------------------------------


class TestResolveFallbackCall:
    def test_the_class_resolves_against_the_fallback_provider(self):
        r = resolve_fallback_call(
            LLMCallSpec(intelligence_class="standard-high", caller="unit"), _config(), CLASSES
        )
        assert (r.provider, r.model, r.extras) == ("openai", "gpt-6", {"reasoning_effort": "xhigh"})
        assert (r.api_key, r.credential, r.caller) == ("sk-fallback", "fallback", "unit")

    def test_the_fallback_brings_its_own_endpoint(self):
        r = resolve_fallback_call(
            LLMCallSpec(intelligence_class="fast-low"),
            _config(base_url="https://llm.example/v1"),
            CLASSES,
        )
        assert r.base_url == "https://llm.example/v1"

    def test_max_tokens_is_the_one_thing_shared_with_the_primary(self):
        cfg = replace(_config(), max_tokens=321)
        assert resolve_fallback_call(LLMCallSpec(), cfg, CLASSES).max_tokens == 321
        assert resolve_fallback_call(LLMCallSpec(max_tokens=9), cfg, CLASSES).max_tokens == 9

    def test_a_call_naming_no_class_uses_the_fallback_default_class(self):
        r = resolve_fallback_call(LLMCallSpec(), _config(default_class="standard-high"), CLASSES)
        assert r.model == "gpt-6"

    def test_without_a_fallback_default_class_the_fallback_model_is_used(self):
        # The primary's default_class (fast-low) is not borrowed: the block is its own.
        r = resolve_fallback_call(LLMCallSpec(), _config(model="gpt-6-nano"), CLASSES)
        assert (r.model, r.extras) == ("gpt-6-nano", {})

    def test_a_class_with_no_slice_on_the_fallback_provider_refuses(self):
        with pytest.raises(NoFallbackRoute, match="'anthropic-only' has no slice.*'openai'"):
            resolve_fallback_call(
                LLMCallSpec(intelligence_class="anthropic-only"), _config(model="gpt-6"), CLASSES
            )

    def test_an_unknown_class_falls_back_to_the_fallback_model(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.llm.spec"):
            r = resolve_fallback_call(
                LLMCallSpec(intelligence_class="nope"), _config(model="gpt-6-nano"), CLASSES
            )
        assert r.model == "gpt-6-nano"
        assert "unknown intelligence class" in caplog.text

    def test_an_explicit_model_for_another_vendor_refuses(self):
        with pytest.raises(NoFallbackRoute, match="'claude-opus-5'"):
            resolve_fallback_call(LLMCallSpec(model="claude-opus-5"), _config(), CLASSES)

    def test_an_explicit_model_is_honoured_on_a_second_key_for_the_same_vendor(self):
        cfg = LLMConfig(
            provider="anthropic",
            api_key="sk-ant-one",
            fallback=LLMFallbackConfig(provider="anthropic", api_key="sk-ant-two"),
        )
        r = resolve_fallback_call(LLMCallSpec(model="claude-opus-5"), cfg, CLASSES)
        assert (r.provider, r.model, r.api_key) == ("anthropic", "claude-opus-5", "sk-ant-two")

    def test_no_fallback_configured_refuses(self):
        with pytest.raises(NoFallbackRoute):
            resolve_fallback_call(LLMCallSpec(), LLMConfig(), CLASSES)

    def test_the_cache_key_separates_credentials(self):
        """A second key for the same vendor must not be served the primary's adapter."""
        cfg = LLMConfig(
            provider="anthropic",
            api_key="sk-ant-one",
            fallback=LLMFallbackConfig(provider="anthropic", api_key="sk-ant-two"),
        )
        spec = LLMCallSpec(intelligence_class="fast-low")
        primary = resolve_call(spec, cfg, CLASSES)
        fallback = resolve_fallback_call(spec, cfg, CLASSES)
        assert (primary.provider, primary.model) == (fallback.provider, fallback.model)
        assert primary.cache_key != fallback.cache_key


# -- the client ----------------------------------------------------------------


class Factory:
    """One FakeProvider per credential, recording what each build asked for."""

    def __init__(self) -> None:
        self.by_key: dict[str, FakeProvider] = {}
        self.builds: list[dict] = []

    def __call__(self, **kw: Any) -> FakeProvider:
        self.builds.append(kw)
        return self.by_key.setdefault(kw["api_key"], FakeProvider(model_name=kw["model"]))

    def fake(self, api_key: str) -> FakeProvider:
        return self.by_key.setdefault(api_key, FakeProvider())


def _client(config: LLMConfig | None = None) -> tuple[LLMClient, Factory, list]:
    factory = Factory()
    outcomes: list[tuple[str, dict]] = []
    client = LLMClient(
        config or _config(), classes_loader=lambda: CLASSES, provider_factory=factory
    )
    client.on_outcome = lambda signal, detail: outcomes.append((signal, detail))
    return client, factory, outcomes


async def test_the_fallback_serves_while_the_primary_is_unavailable():
    client, factory, _ = _client()
    factory.fake("sk-fallback").add_text("from the fallback")
    client.availability_gate = lambda: BLOCKED

    resp = await client.complete("hi", spec=LLMCallSpec(intelligence_class="standard-high"))

    assert resp.text == "from the fallback"
    assert factory.fake("sk-ant-primary").calls == []
    [build] = factory.builds
    assert (build["provider"], build["model"], build["api_key"]) == (
        "openai",
        "gpt-6",
        "sk-fallback",
    )
    assert build["extras"] == {"reasoning_effort": "xhigh"}


async def test_the_primary_is_used_whenever_it_is_available():
    client, factory, _ = _client()
    factory.fake("sk-ant-primary").add_text("primary")
    client.availability_gate = lambda: None
    assert (await client.complete("hi")).text == "primary"
    assert factory.fake("sk-fallback").calls == []


async def test_a_class_the_fallback_cannot_serve_fails_fast_naming_both_reasons():
    client, factory, _ = _client()
    client.availability_gate = lambda: BLOCKED

    with pytest.raises(ProviderUnavailableError) as raised:
        await client.complete("hi", spec=LLMCallSpec(intelligence_class="anthropic-only"))

    assert BLOCKED in str(raised.value)
    assert "no slice" in str(raised.value)
    assert factory.builds == []  # no adapter built, no vendor called


async def test_without_a_fallback_the_refusal_is_unchanged():
    client, factory, _ = _client(LLMConfig(provider="anthropic", api_key="sk-ant-primary"))
    client.availability_gate = lambda: BLOCKED
    with pytest.raises(ProviderUnavailableError) as raised:
        await client.complete("hi")
    assert str(raised.value) == BLOCKED
    assert factory.builds == []


async def test_fallback_calls_are_not_evidence_about_the_primary():
    """A fallback success must never read as the primary credential recovering."""
    client, factory, outcomes = _client()
    fallback = factory.fake("sk-fallback")
    fallback.add_text("ok")
    client.availability_gate = lambda: BLOCKED

    await client.complete("hi")
    with pytest.raises(RuntimeError):  # the fallback's queue is empty now
        await client.complete("again")

    assert outcomes == []
    client.availability_gate = lambda: None
    factory.fake("sk-ant-primary").add_text("primary")
    await client.complete("hi")
    assert outcomes == [("ok", {})]


async def test_the_switch_is_logged_once_in_each_direction(caplog):
    client, factory, _ = _client()
    for _ in range(3):
        factory.fake("sk-fallback").add_text("f")
    factory.fake("sk-ant-primary").add_text("p")
    gate = {"blocked": BLOCKED}
    client.availability_gate = lambda: gate["blocked"]

    with caplog.at_level(logging.INFO, logger="src.llm.client"):
        await client.complete("1")
        await client.complete("2")
        gate["blocked"] = None
        await client.complete("3")

    messages = [r.getMessage() for r in caplog.records]
    assert sum("use llm.fallback" in m for m in messages) == 1
    assert sum("primary credential is back" in m for m in messages) == 1


async def test_a_tool_loop_moves_between_credentials_turn_by_turn():
    """The gate is read on every call, so a loop that outlives an outage recovers."""
    client, factory, _ = _client()
    primary = factory.fake("sk-ant-primary")
    fallback = factory.fake("sk-fallback")
    tool_turn = ChatResponse(content=[ToolUseBlock(id="t1", name="lookup", input={})])
    primary.add_response(tool_turn)
    fallback.add_response(ChatResponse(content=[ToolUseBlock(id="t2", name="lookup", input={})]))
    primary.add_text("done")
    states = iter([None, BLOCKED, None])
    client.availability_gate = lambda: next(states)

    async def execute(name: str, args: dict) -> dict:
        return {"success": True}

    run = await client.run_tools("go", [{"name": "lookup", "input_schema": {}}], execute)

    assert run.stopped_by == "done" and run.text == "done"
    assert len(primary.calls) == 2 and len(fallback.calls) == 1
    # The fallback saw the whole transcript so far, including the primary's turn.
    assert len(fallback.calls[0].messages) == 3


def test_resolve_current_names_what_a_call_would_run_on():
    client, _, _ = _client()
    spec = LLMCallSpec(intelligence_class="standard-high")
    client.availability_gate = lambda: None
    assert client.resolve_current(spec).model == "claude-opus-5"
    client.availability_gate = lambda: BLOCKED
    assert client.resolve_current(spec).model == "gpt-6"
    # A refusal makes no call at all; the primary resolution is what ``resolve`` says.
    unserved = LLMCallSpec(intelligence_class="anthropic-only")
    assert client.resolve_current(unserved).model == "claude-fable-5-1"


# -- the playbook llm step ---------------------------------------------------


async def test_a_playbook_llm_step_completes_on_the_fallback_during_an_outage():
    from tests.test_llm_executor import (
        LiveLlmExecutor,
        ProfileStore,
        context,
        llm_step,
        profile_with_class,
    )

    client, factory, _ = _client()
    factory.fake("sk-fallback").add_text('{"risk": "low"}', usage=TokenUsage(10, 2, True))
    client.availability_gate = lambda: BLOCKED
    ctx = context(FakeProvider(), db=ProfileStore(profile_with_class("standard-high")))
    ctx = replace(ctx, services=replace(ctx.services, llm=client))

    result = await LiveLlmExecutor().execute(llm_step(), ctx)

    assert result.outcome == "low"
    assert result.operation == "llm:worker/gpt-6"


async def test_a_playbook_llm_step_the_fallback_cannot_serve_is_a_provider_error():
    from tests.test_llm_executor import (
        LiveLlmExecutor,
        ProfileStore,
        context,
        llm_step,
        profile_with_class,
    )

    client, factory, _ = _client()
    client.availability_gate = lambda: BLOCKED
    ctx = context(FakeProvider(), db=ProfileStore(profile_with_class("anthropic-only")))
    ctx = replace(ctx, services=replace(ctx.services, llm=client))
    # The step's budget pre-check builds the primary adapter; give it usage so
    # the step reaches the call.
    factory.fake("sk-ant-primary").add_text("unused", usage=TokenUsage(1, 1, True))

    result = await LiveLlmExecutor().execute(llm_step(), ctx)

    assert result.outcome == "provider_error"
    assert result.diagnostics == ("provider_unavailable",)
    assert factory.fake("sk-fallback").calls == []
