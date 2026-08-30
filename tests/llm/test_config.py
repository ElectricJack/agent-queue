"""LLMConfig — the `llm:` block and legacy `chat_provider:` mapping (spec §4.1)."""

from __future__ import annotations

import logging

import yaml

from src.config import (
    LLM_PROVIDER_IDS,
    AppConfig,
    LLMConfig,
    load_config,
    normalize_llm_provider,
)


def _write(tmp_path, mapping: dict) -> str:
    mapping.setdefault("discord", {"bot_token": "tok", "guild_id": "123"})
    mapping.setdefault("database_path", str(tmp_path / "test.db"))
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump(mapping))
    return str(p)


class TestLLMConfigDataclass:
    def test_defaults(self):
        cfg = LLMConfig()
        assert cfg.provider == "anthropic"
        assert cfg.model == ""
        assert cfg.max_tokens == 4096
        assert cfg.default_class == ""
        assert cfg.validate() == []

    def test_provider_ids(self):
        assert LLM_PROVIDER_IDS == frozenset({"anthropic", "google", "openai"})

    def test_numeric_model_coerced_to_str(self):
        assert LLMConfig(model=4).model == "4"  # type: ignore[arg-type]

    def test_unknown_provider_rejected(self):
        errs = LLMConfig(provider="gemini").validate()
        assert any(e.section == "llm" and e.field == "provider" for e in errs)

    def test_openai_needs_base_url_or_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        errs = LLMConfig(provider="openai").validate()
        assert any(e.field == "base_url" for e in errs)
        assert LLMConfig(provider="openai", base_url="http://localhost:11434/v1").validate() == []
        assert LLMConfig(provider="openai", api_key="sk").validate() == []
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        assert LLMConfig(provider="openai").validate() == []

    def test_normalize_legacy_ids(self):
        assert normalize_llm_provider("gemini") == "google"
        assert normalize_llm_provider("ollama") == "openai"
        assert normalize_llm_provider("anthropic") == "anthropic"
        assert normalize_llm_provider("google") == "google"


class TestLoadLLMBlock:
    def test_llm_block_loads(self, tmp_path):
        path = _write(
            tmp_path,
            {
                "llm": {
                    "provider": "google",
                    "model": "gemini-2.5-flash",
                    "max_tokens": 1234,
                    "default_class": "fast-low",
                }
            },
        )
        cfg = load_config(path)
        assert cfg.llm == LLMConfig(
            provider="google", model="gemini-2.5-flash", max_tokens=1234, default_class="fast-low"
        )

    def test_legacy_chat_provider_maps_with_warning(self, tmp_path, caplog):
        path = _write(tmp_path, {"chat_provider": {"provider": "gemini", "model": "gemini-2.5-flash"}})
        with caplog.at_level(logging.WARNING, logger="src.config"):
            cfg = load_config(path)
        assert cfg.llm.provider == "google"
        assert cfg.llm.model == "gemini-2.5-flash"
        assert any("chat_provider" in r.message and "deprecated" in r.message for r in caplog.records)

    def test_legacy_ollama_maps_to_openai(self, tmp_path):
        path = _write(
            tmp_path,
            {"chat_provider": {"provider": "ollama", "model": "qwen3",
                               "base_url": "http://localhost:11434/v1"}},
        )
        cfg = load_config(path)
        assert cfg.llm.provider == "openai"
        assert cfg.llm.base_url == "http://localhost:11434/v1"
        assert cfg.llm.model == "qwen3"

    def test_both_blocks_llm_wins(self, tmp_path, caplog):
        path = _write(
            tmp_path,
            {
                "llm": {"provider": "anthropic", "model": "claude-sonnet-5"},
                "chat_provider": {"provider": "gemini"},
            },
        )
        with caplog.at_level(logging.WARNING, logger="src.config"):
            cfg = load_config(path)
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.model == "claude-sonnet-5"
        assert any("ignoring" in r.message for r in caplog.records)

    def test_unknown_keys_ignored(self, tmp_path):
        path = _write(
            tmp_path,
            {"llm": {"provider": "anthropic", "keep_alive": "1h", "thinking_budget": 8192}},
        )
        cfg = load_config(path)
        assert cfg.llm.provider == "anthropic"

    def test_no_block_gives_defaults(self, tmp_path):
        cfg = load_config(_write(tmp_path, {}))
        assert cfg.llm == LLMConfig()

    def test_appconfig_validate_includes_llm(self):
        cfg = AppConfig(llm=LLMConfig(provider="bogus"))
        assert any(e.section == "llm" for e in cfg.validate())
