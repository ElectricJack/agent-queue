from __future__ import annotations

import logging

from src.config import LLMConfig
from src.intelligence_classes import IntelligenceClass
from src.llm.spec import LLMCallSpec, resolve_call, spec_from_llm_config

CLASSES = {
    "fast-low": IntelligenceClass(
        id="fast-low",
        name="Fast · Low",
        description="",
        mapping={
            "anthropic": {"model": "claude-haiku-4-5", "thinking": "low"},
            "google": {"model": "gemini-2.5-flash", "thinking_budget": 1024},
        },
    ),
}


def test_explicit_model_wins_over_class():
    r = resolve_call(
        LLMCallSpec(model="claude-opus-5", intelligence_class="fast-low"),
        LLMConfig(provider="anthropic"),
        CLASSES,
    )
    assert r.model == "claude-opus-5"
    assert r.extras == {}


def test_class_resolves_model_and_extras_for_provider():
    r = resolve_call(
        LLMCallSpec(intelligence_class="fast-low"),
        LLMConfig(provider="google"),
        CLASSES,
    )
    assert r.model == "gemini-2.5-flash"
    assert r.extras == {"thinking_budget": 1024}


def test_default_class_from_config():
    r = resolve_call(
        LLMCallSpec(),
        LLMConfig(provider="anthropic", default_class="fast-low"),
        CLASSES,
    )
    assert r.model == "claude-haiku-4-5"
    assert r.extras == {"thinking": "low"}


def test_config_model_when_no_class():
    r = resolve_call(
        LLMCallSpec(),
        LLMConfig(provider="anthropic", model="claude-sonnet-5"),
        CLASSES,
    )
    assert r.model == "claude-sonnet-5"


def test_unknown_class_warns_and_falls_through(caplog):
    with caplog.at_level(logging.WARNING, logger="src.llm.spec"):
        r = resolve_call(
            LLMCallSpec(intelligence_class="nope"),
            LLMConfig(provider="anthropic", model="claude-sonnet-5"),
            CLASSES,
        )
    assert r.model == "claude-sonnet-5"
    assert any("nope" in rec.message for rec in caplog.records)


def test_missing_provider_slice_falls_through(caplog):
    with caplog.at_level(logging.WARNING, logger="src.llm.spec"):
        r = resolve_call(
            LLMCallSpec(intelligence_class="fast-low"),
            LLMConfig(provider="openai", base_url="http://x"),
            CLASSES,
        )
    assert r.model == ""  # provider default applies inside the adapter
    assert any("openai" in rec.message for rec in caplog.records)


def test_provider_override_and_legacy_id():
    r = resolve_call(LLMCallSpec(provider="gemini"), LLMConfig(provider="anthropic"), {})
    assert r.provider == "google"


def test_max_tokens_and_caller_and_creds():
    cfg = LLMConfig(provider="openai", base_url="http://b", api_key="k", max_tokens=99)
    r = resolve_call(LLMCallSpec(caller="x"), cfg, {})
    assert (r.max_tokens, r.caller, r.base_url, r.api_key) == (99, "x", "http://b", "k")
    assert resolve_call(LLMCallSpec(max_tokens=5), cfg, {}).max_tokens == 5


def test_spec_from_llm_config():
    s = spec_from_llm_config(
        {
            "provider": "gemini",
            "model": "gemini-2.5-pro",
            "intelligence_class": "fast-low",
            "max_tokens": 10,
            "thinking_budget": 1,
        },
        caller="playbook:x",
    )
    assert s == LLMCallSpec(
        provider="gemini",
        model="gemini-2.5-pro",
        intelligence_class="fast-low",
        max_tokens=10,
        caller="playbook:x",
    )
    assert spec_from_llm_config(None, caller="c", max_tokens=7) == LLMCallSpec(
        max_tokens=7, caller="c"
    )
