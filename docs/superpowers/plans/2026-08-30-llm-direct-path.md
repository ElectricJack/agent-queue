# LLM Direct Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `Supervisor.chat()` + `src/chat_providers/` + `config.chat_provider` with a small generic `src/llm/` client (`complete` / `run_tools`) driven by an `llm:` config block and intelligence classes; delete the in-process Supervisor and everything that only served it.

**Architecture:** `LLMClient` (one instance, owned by `Orchestrator`) resolves an `LLMCallSpec` (provider / model / intelligence class) against `config.llm` and the vault intelligence classes, builds a cached provider adapter, and exposes a single-shot `complete()` and a caller-supplied-tools loop `run_tools()`. The five live consumers (playbook nodes + transitions, plugin `invoke_llm`, stub enricher, vault summaries) are ported onto it; then `src/runtimes/supervisor.py`, `src/chat_providers/`, plan discovery, and the dead Supervisor methods are deleted.

**Tech Stack:** Python 3.12, asyncio, dataclasses, pytest (`pytest tests/ -n auto`, Postgres on :5533 via docker compose), ruff (line-length 100). Optional SDKs: `anthropic`, `google-genai`, `openai` (all lazily imported).

**Spec:** `docs/superpowers/specs/2026-08-30-llm-direct-path-design.md`

## Deviations from the spec (found while excerpting; apply these, and Task 15 amends the spec)

1. **`src/runtimes/base.py` and the `RuntimeRegistry` stay; only `supervisor.py` is deleted.** 27 test files (`test_orchestrator.py`, checkpoint, merge-slot, reaper, HITL…) dispatch tasks through `Orchestrator(config, runtimes=MockAdapterFactory())`, i.e. the `Runtime` ABC is the orchestrator's test seam for non-session execution, and `src/orchestrator/sync_workflow.py:266-278` uses it too. Spec L6's intent — "harness is the only selector, no in-process runtime" — is met by: `default_registry()` registers **nothing**, `config.default_runtime` is deleted, and `## Config.runtime` is rejected by the profile parser.
2. **The LLM plan parser is deleted, not ported.** `Orchestrator._chat_provider` (`core.py:421-431`) is built but never read anywhere (`_generate_tasks_from_plan` exists only in a comment). `auto_task.use_llm_parser` and `auto_task.llm_parser_model` go; no `llm_parser_class` is added.
3. **No `usage` field on `LLMResponse` / `LLMRunResult`.** The adapters' `ChatResponse` carries content blocks only; token accounting for playbook nodes keeps using `_estimate_tokens` as today. `LLMRunResult.tool_calls_made` is added instead (the runner logs it).
4. **`AgentProfile.runtime` (dataclass field + `agent_profiles.runtime` column) stays**, deprecated and always `""`. Dropping the column would need an Alembic migration for no behavioural gain. The parser rejects the key in `## Config`, so no new row can carry a value.

## Global Constraints

- Line length 100, ruff, py312. Async-first. Commands return `{"success": bool, ...}` dicts.
- Provider ids are exactly `anthropic | google | openai`; legacy `gemini → google`, `ollama → openai`.
- Config block is `llm:`; legacy `chat_provider:` loads with one deprecation warning; unknown keys in existing YAML are ignored, never rejected.
- `LLMCallSpec` resolution order: explicit `model` > intelligence class (`spec.intelligence_class` or `config.llm.default_class`) > `config.llm.model` > provider built-in default. Unknown class / missing provider slice → warning + fall through, never a hard failure.
- Tool-loop rule: an exception from the executor becomes a `{"success": false, "error": ...}` tool result and never aborts the loop.
- Every LLM call is logged via `LLMLogger.log_llm_call(caller=...)` to `llm.jsonl`.
- Nothing outside `src/llm/` constructs a provider adapter.
- Work on branch `llm-direct-path` in a worktree (`superpowers:using-git-worktrees`), never on `main`.
- Test command for every task: `timeout 580 /home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/ -n auto -q -p no:cacheprovider` (full) or the file-scoped form shown in the task. The venv python is `/home/jkern/dev/agent-queue2/.venv/bin/python`; `python` alone is not on PATH.
- Each phase's last task ends with the full suite green; commit boundaries below are the four spec §9 commits.

---

# Phase 1 — add `src/llm/` (spec §3, §4.1, §8)

### Task 1: `LLMConfig` + `llm:` loader with legacy `chat_provider:` mapping

**Files:**
- Modify: `src/config.py` (new dataclass next to `ChatProviderConfig` at :585; field on `AppConfig` after :1388; `validate()` at :1571; `RESTART_REQUIRED_SECTIONS` :1730; `_SECTION_FIELDS` :1757; loader after :2212)
- Test: `tests/llm/__init__.py` (empty), `tests/llm/test_config.py`

**Interfaces:**
- Produces: `src.config.LLMConfig(provider, model, api_key, base_url, max_tokens, default_class)`, `src.config.LLM_PROVIDER_IDS`, `src.config.normalize_llm_provider(name) -> str`, `AppConfig.llm: LLMConfig`. `ChatProviderConfig` and `AppConfig.chat_provider` remain untouched until Task 14.

- [ ] **Step 1: Write the failing tests**

```python
# tests/llm/test_config.py
"""LLMConfig — the `llm:` block and legacy `chat_provider:` mapping (spec §4.1)."""

from __future__ import annotations

import logging

import pytest
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

    def test_legacy_ollama_gets_default_base_url(self, tmp_path):
        path = _write(tmp_path, {"chat_provider": {"provider": "ollama", "model": "qwen3"}})
        cfg = load_config(path)
        assert cfg.llm.provider == "openai"
        assert cfg.llm.base_url == "http://localhost:11434/v1"

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
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/llm/test_config.py -q`
Expected: ImportError on `LLMConfig`.

- [ ] **Step 3: Implement**

In `src/config.py`, directly **above** `class ChatProviderConfig` (line 585):

```python
LLM_PROVIDER_IDS = frozenset({"anthropic", "google", "openai"})
_LEGACY_LLM_PROVIDER_IDS = {"gemini": "google", "ollama": "openai"}


def normalize_llm_provider(name: str) -> str:
    """Map legacy chat_provider ids (``gemini``, ``ollama``) to ``llm`` ids."""
    return _LEGACY_LLM_PROVIDER_IDS.get(name, name)


@dataclass
class LLMConfig:
    """The direct LLM path (``src/llm``): playbook nodes and transitions, plugin
    ``invoke_llm``, stub enrichment, vault summaries.  Not the coding agents —
    those run as tmux sessions selected by the profile's ``harness``."""

    provider: str = "anthropic"  # "anthropic" | "google" | "openai"
    model: str = ""  # explicit model id; empty = intelligence class, else provider default
    api_key: str = ""  # optional; ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY otherwise
    base_url: str = ""  # openai only: OpenAI-compatible endpoint (Ollama: http://localhost:11434/v1)
    max_tokens: int = 4096
    default_class: str = ""  # intelligence class used when a call names none

    def __post_init__(self) -> None:
        # YAML may parse ``model: 4`` as an int; APIs require a string.
        if self.model and not isinstance(self.model, str):
            object.__setattr__(self, "model", str(self.model))

    def validate(self) -> list[ConfigError]:
        errors: list[ConfigError] = []
        if self.provider not in LLM_PROVIDER_IDS:
            errors.append(
                ConfigError(
                    "llm",
                    "provider",
                    f"must be one of {sorted(LLM_PROVIDER_IDS)}, got '{self.provider}'",
                )
            )
        if self.provider == "openai" and not (
            self.base_url or self.api_key or os.environ.get("OPENAI_API_KEY")
        ):
            errors.append(
                ConfigError(
                    "llm",
                    "base_url",
                    "provider 'openai' needs base_url (a local OpenAI-compatible endpoint) "
                    "or an API key (api_key / OPENAI_API_KEY)",
                )
            )
        return errors
```

On `AppConfig`, right after line 1388 (`chat_provider: ChatProviderConfig = ...`):

```python
    llm: LLMConfig = field(default_factory=LLMConfig)
```

In `AppConfig.validate()` after line 1571 (`errors.extend(self.chat_provider.validate())`):

```python
        errors.extend(self.llm.validate())
```

Add `"llm",` to `RESTART_REQUIRED_SECTIONS` (after `"chat_provider",` at :1736) and to `_SECTION_FIELDS` (after `"chat_provider",` at :1768).

Add a module-level helper directly above `def load_config(` (:2026):

```python
def _llm_config_from_mapping(m: dict, *, legacy: bool) -> LLMConfig:
    raw_provider = str(m.get("provider", "anthropic") or "anthropic")
    provider = normalize_llm_provider(raw_provider)
    raw_model = m.get("model", "")
    base_url = str(m.get("base_url", "") or "")
    if legacy and raw_provider == "ollama" and not base_url:
        base_url = "http://localhost:11434/v1"
    return LLMConfig(
        provider=provider,
        model=str(raw_model) if raw_model else "",
        api_key=str(m.get("api_key", "") or ""),
        base_url=base_url,
        max_tokens=int(m.get("max_tokens", 4096)),
        default_class=str(m.get("default_class", "") or ""),
    )
```

In the loader, directly **after** the `if "chat_provider" in raw:` block (ends :2212):

```python
    llm_raw = raw.get("llm")
    legacy_raw = raw.get("chat_provider")
    if isinstance(llm_raw, dict):
        if isinstance(legacy_raw, dict):
            logger.warning(
                "%s: both 'llm:' and legacy 'chat_provider:' are present — using 'llm:' "
                "and ignoring 'chat_provider:'",
                path,
            )
        config.llm = _llm_config_from_mapping(llm_raw, legacy=False)
    elif isinstance(legacy_raw, dict):
        logger.warning(
            "%s: 'chat_provider:' is deprecated — rename the block to 'llm:' "
            "(provider ids: gemini→google, ollama→openai)",
            path,
        )
        config.llm = _llm_config_from_mapping(legacy_raw, legacy=True)
```

- [ ] **Step 4: Run tests**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/llm/test_config.py tests/test_config_validation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/llm/
git commit -m "feat(config): LLMConfig and the llm: block, legacy chat_provider: mapped with a warning"
```

---

### Task 2: Move provider adapters to `src/llm/providers/`, add `FakeProvider` and the factory

**Files:**
- Create (via `git mv`): `src/llm/__init__.py`, `src/llm/types.py` (← `src/chat_providers/types.py`), `src/llm/tool_conversion.py` (← `tool_conversion.py`), `src/llm/providers/__init__.py`, `src/llm/providers/base.py` (← `base.py`), `src/llm/providers/anthropic.py` (← `anthropic.py`), `src/llm/providers/google.py` (← `gemini.py`), `src/llm/providers/openai.py` (← `ollama.py`), `src/llm/providers/adapters/` (← `adapters/`)
- Create: `src/llm/fake.py`
- Modify: `src/chat_providers/__init__.py`, `src/chat_providers/base.py`, `types.py`, `logged.py`, `anthropic.py`, `gemini.py`, `ollama.py` become one-line re-export shims (deleted in Task 14) so nothing breaks during coexistence.
- Test: `tests/llm/test_providers.py`

**Interfaces:**
- Produces: `src.llm.providers.base.LLMProvider` (the ABC; `create_message(*, messages, system, tools=None, max_tokens=1024) -> ChatResponse`, `model_name`, `is_model_loaded()`, `is_configured` property default `True`), `src.llm.providers.create_provider(resolved: ResolvedCall) -> LLMProvider` (ResolvedCall defined in Task 3 — for this task it takes keyword fields, see code), `src.llm.fake.FakeProvider`, `src.llm.types.{ChatResponse, TextBlock, ToolUseBlock, serialize_canonical}`.

- [ ] **Step 1: Move files**

```bash
mkdir -p src/llm/providers
git mv src/chat_providers/types.py src/llm/types.py
git mv src/chat_providers/tool_conversion.py src/llm/tool_conversion.py
git mv src/chat_providers/base.py src/llm/providers/base.py
git mv src/chat_providers/anthropic.py src/llm/providers/anthropic.py
git mv src/chat_providers/gemini.py src/llm/providers/google.py
git mv src/chat_providers/ollama.py src/llm/providers/openai.py
git mv src/chat_providers/adapters src/llm/providers/adapters
touch src/llm/__init__.py src/llm/providers/__init__.py
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/llm/test_providers.py
"""Provider package layout, FakeProvider, and the factory (spec §3)."""

from __future__ import annotations

import pytest

from src.llm.fake import FakeProvider
from src.llm.providers import create_provider
from src.llm.providers.base import LLMProvider
from src.llm.types import ChatResponse, TextBlock, ToolUseBlock


class TestFakeProvider:
    async def test_fifo_text_and_records_calls(self):
        fake = FakeProvider()
        fake.add_text("one")
        fake.add_text("two")
        r1 = await fake.create_message(messages=[{"role": "user", "content": "a"}], system="s")
        r2 = await fake.create_message(messages=[{"role": "user", "content": "b"}], system="s")
        assert r1.text_parts == ["one"] and r2.text_parts == ["two"]
        assert [c.messages[0]["content"] for c in fake.calls] == ["a", "b"]
        assert fake.model_name == "fake"
        assert isinstance(fake, LLMProvider)

    async def test_tool_call_helper(self):
        fake = FakeProvider()
        fake.add_tool_call("list_tasks", {"project_id": "p"})
        resp = await fake.create_message(messages=[], system="")
        assert resp.has_tool_use
        assert resp.tool_uses[0].name == "list_tasks"
        assert resp.tool_uses[0].input == {"project_id": "p"}

    async def test_exhausted_queue_raises(self):
        fake = FakeProvider()
        with pytest.raises(RuntimeError, match="no scripted response"):
            await fake.create_message(messages=[], system="")


class TestFactory:
    def test_unknown_provider_rejected(self):
        with pytest.raises(ValueError, match="unknown llm provider"):
            create_provider(provider="nope", model="m", base_url="", api_key="", extras={})

    def test_openai_uses_base_url(self):
        pytest.importorskip("openai")
        p = create_provider(
            provider="openai", model="qwen3", base_url="http://localhost:11434/v1",
            api_key="", extras={},
        )
        assert p.model_name == "qwen3"
        assert type(p).__name__ == "OpenAIProvider"

    def test_google_class_name(self):
        pytest.importorskip("google.genai")
        p = create_provider(provider="google", model="gemini-2.5-flash", base_url="", api_key="k", extras={})
        assert type(p).__name__ == "GoogleProvider"
        assert p.model_name == "gemini-2.5-flash"

    def test_anthropic_default_model(self, monkeypatch):
        pytest.importorskip("anthropic")
        for var in ("GOOGLE_CLOUD_PROJECT", "ANTHROPIC_VERTEX_PROJECT_ID", "AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        p = create_provider(provider="anthropic", model="", base_url="", api_key="sk-test", extras={})
        assert type(p).__name__ == "AnthropicProvider"
        assert p.model_name == "claude-sonnet-5"


def test_types_reexported():
    assert ChatResponse(content=[TextBlock(text="x"), ToolUseBlock(id="1", name="t", input={})]).has_tool_use
```

- [ ] **Step 3: Run to verify failure**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/llm/test_providers.py -q`
Expected: ImportError (`src.llm.fake`).

- [ ] **Step 4: Implement**

`src/llm/providers/base.py` — rename the class and fix the import:

```python
"""Common interface for the direct-LLM provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.llm.types import ChatResponse


class LLMProvider(ABC):
    @abstractmethod
    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...

    @property
    def is_configured(self) -> bool:
        """False when credentials are missing; the client reports it without calling."""
        return True

    async def is_model_loaded(self) -> bool:
        return True
```

`src/llm/providers/anthropic.py`: change `from .base import ChatProvider` → `from .base import LLMProvider`, `from .types import` → `from src.llm.types import`, class `AnthropicChatProvider(ChatProvider)` → `AnthropicProvider(LLMProvider)`. Replace every hardcoded `"claude-sonnet-4-20250514"` / `"claude-sonnet-4@20250514"` default with `"claude-sonnet-5"`. Change the constructor signature to `def __init__(self, model: str = "", api_key: str = "", thinking_budget: int = 0)`: in the "Try explicit API key" branch use `api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")`; store `self._thinking_budget = thinking_budget`. In `create_message`, after building `kwargs`, add:

```python
        if self._thinking_budget > 0:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self._thinking_budget}
            kwargs["max_tokens"] = max(max_tokens, self._thinking_budget + 1024)
```

`src/llm/providers/google.py`: same import fixes; class `GeminiChatProvider` → `GoogleProvider(LLMProvider)`; the `adapters.gemini_adapter` import becomes `from src.llm.providers.adapters import gemini_adapter` (keep the module name). Constructor unchanged (`model, api_key, thinking_budget`).

`src/llm/providers/openai.py`: same import fixes; class `OllamaChatProvider` → `OpenAIProvider(LLMProvider)`. Constructor becomes:

```python
    def __init__(
        self,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        keep_alive: str = "1h",
        num_ctx: int = 0,
        reasoning_effort: str = "",
    ):
        from openai import AsyncOpenAI

        self._is_local = bool(base_url) and "api.openai.com" not in base_url
        self._client = AsyncOpenAI(
            base_url=base_url or None,
            api_key=api_key or os.environ.get("OPENAI_API_KEY") or ("ollama" if self._is_local else ""),
        )
        self._model = str(model) if model else ("qwen3.5:35b" if self._is_local else "gpt-5")
        self._reasoning_effort = reasoning_effort
        self._keep_alive = keep_alive
        self._num_ctx = num_ctx
        self._keep_alive_seconds = self._parse_duration(keep_alive)
        self._last_request_at: float = 0.0
        self._ollama_api_root = (base_url or "").rstrip("/").removesuffix("/v1")
```

and in `create_message` only attach `extra_body` when `self._is_local` (Ollama options), and add `kwargs["reasoning_effort"] = self._reasoning_effort` when set. `is_model_loaded()` returns `True` immediately when `not self._is_local`. Add `import os` if missing.

`src/llm/providers/adapters/*.py`: fix `from ..types import` → `from src.llm.types import`.

`src/llm/providers/__init__.py`:

```python
"""Provider adapters for the direct LLM path.  Only ``create_provider`` is
used outside this package; nothing else constructs an adapter."""

from __future__ import annotations

from .base import LLMProvider


def create_provider(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    extras: dict,
) -> LLMProvider:
    """Build one adapter.  ``extras`` is the intelligence-class slice minus ``model``
    (``thinking``, ``thinking_budget``, ``reasoning_effort``); unknown keys are ignored."""
    if provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(
            model=model,
            api_key=api_key,
            thinking_budget=_anthropic_budget(extras.get("thinking")),
        )
    if provider == "google":
        from .google import GoogleProvider

        return GoogleProvider(
            model=model or "gemini-2.5-flash",
            api_key=api_key,
            thinking_budget=int(extras.get("thinking_budget", 8192)),
        )
    if provider == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider(
            model=model,
            base_url=base_url,
            api_key=api_key,
            reasoning_effort=str(extras.get("reasoning_effort", "") or ""),
        )
    raise ValueError(f"unknown llm provider {provider!r}")


_THINKING_BUDGETS = {"off": 0, "low": 1024, "medium": 4096, "high": 16000}


def _anthropic_budget(level) -> int:
    if isinstance(level, int):
        return level
    return _THINKING_BUDGETS.get(str(level or "off"), 0)


__all__ = ["LLMProvider", "create_provider"]
```

`src/llm/fake.py`:

```python
"""FakeProvider — scripted responses for tests and dry runs."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from src.llm.providers.base import LLMProvider
from src.llm.types import ChatResponse, TextBlock, ToolUseBlock


@dataclass
class RecordedCall:
    messages: list[dict]
    system: str
    tools: list[dict] | None
    max_tokens: int
    timestamp: float = field(default_factory=time.time)


class FakeProvider(LLMProvider):
    """Returns queued ``ChatResponse`` objects FIFO and records every call."""

    def __init__(self, model_name: str = "fake"):
        self._queue: list[ChatResponse] = []
        self.calls: list[RecordedCall] = []
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    def add_response(self, response: ChatResponse) -> None:
        self._queue.append(response)

    def add_text(self, text: str) -> None:
        self._queue.append(ChatResponse(content=[TextBlock(text=text)]))

    def add_tool_call(self, name: str, args: dict | None = None) -> None:
        self._queue.append(
            ChatResponse(
                content=[ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=args or {})]
            )
        )

    def add_tool_calls(self, calls: list[tuple[str, dict]]) -> None:
        self._queue.append(
            ChatResponse(
                content=[
                    ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:12]}", name=n, input=a)
                    for n, a in calls
                ]
            )
        )

    async def create_message(
        self,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        self.calls.append(RecordedCall(list(messages), system, tools, max_tokens))
        if not self._queue:
            raise RuntimeError("FakeProvider: no scripted response left")
        return self._queue.pop(0)
```

Shims in `src/chat_providers/` (temporary, deleted in Task 14): `base.py` → `from src.llm.providers.base import LLMProvider as ChatProvider  # noqa: F401`; `types.py` → `from src.llm.types import *  # noqa: F401,F403` plus `from src.llm.types import serialize_canonical  # noqa: F401`; `anthropic.py` → `from src.llm.providers.anthropic import AnthropicProvider as AnthropicChatProvider  # noqa: F401`; `gemini.py` → `from src.llm.providers.google import GoogleProvider as GeminiChatProvider  # noqa: F401`; `ollama.py` → `from src.llm.providers.openai import OpenAIProvider as OllamaChatProvider  # noqa: F401`; `logged.py` unchanged except its imports (`from .base import ChatProvider` still resolves through the shim; `from .types import ChatResponse, serialize_canonical` too). In `src/chat_providers/__init__.py::create_chat_provider` keep the body but build through the shims — it stays working for `Supervisor` until Task 12: replace the three constructions with

```python
    from src.llm.providers import create_provider
    from src.config import normalize_llm_provider

    provider_id = normalize_llm_provider(config.provider)
    base_url = config.base_url or ("http://localhost:11434/v1" if config.provider == "ollama" else "")
    extras = {"thinking_budget": config.thinking_budget} if provider_id == "google" else {}
    try:
        provider = create_provider(
            provider=provider_id, model=config.model, base_url=base_url,
            api_key=config.api_key, extras=extras,
        )
    except Exception:
        return None
    return provider if provider.is_configured else None
```

- [ ] **Step 5: Run tests**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/llm/ tests/test_logged_provider.py tests/test_supervisor.py -q`
Expected: PASS (the shims keep the old suite green).

- [ ] **Step 6: Commit**

```bash
git add -A src/llm src/chat_providers tests/llm
git commit -m "refactor(llm): move provider adapters under src/llm/providers, add FakeProvider and create_provider"
```

---

### Task 3: `LLMCallSpec` and resolution

**Files:**
- Create: `src/llm/spec.py`
- Test: `tests/llm/test_spec.py`

**Interfaces:**
- Produces: `LLMCallSpec(provider=None, model=None, intelligence_class=None, max_tokens=None, caller="llm")` (frozen dataclass), `ResolvedCall(provider, model, base_url, api_key, max_tokens, extras, caller)`, `resolve_call(spec, config: LLMConfig, classes: dict[str, IntelligenceClass]) -> ResolvedCall`, `spec_from_llm_config(d: dict | None, *, caller: str, max_tokens: int | None = None) -> LLMCallSpec`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/llm/test_spec.py
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
    r = resolve_call(LLMCallSpec(intelligence_class="fast-low"), LLMConfig(provider="google"), CLASSES)
    assert r.model == "gemini-2.5-flash"
    assert r.extras == {"thinking_budget": 1024}


def test_default_class_from_config():
    r = resolve_call(LLMCallSpec(), LLMConfig(provider="anthropic", default_class="fast-low"), CLASSES)
    assert r.model == "claude-haiku-4-5"
    assert r.extras == {"thinking": "low"}


def test_config_model_when_no_class():
    r = resolve_call(LLMCallSpec(), LLMConfig(provider="anthropic", model="claude-sonnet-5"), CLASSES)
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
        r = resolve_call(LLMCallSpec(intelligence_class="fast-low"), LLMConfig(provider="openai", base_url="http://x"), CLASSES)
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
        {"provider": "gemini", "model": "gemini-2.5-pro", "intelligence_class": "fast-low",
         "max_tokens": 10, "thinking_budget": 1},
        caller="playbook:x",
    )
    assert s == LLMCallSpec(provider="gemini", model="gemini-2.5-pro", intelligence_class="fast-low",
                            max_tokens=10, caller="playbook:x")
    assert spec_from_llm_config(None, caller="c", max_tokens=7) == LLMCallSpec(max_tokens=7, caller="c")
```

- [ ] **Step 2: Run to verify failure**

Run: `/home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/llm/test_spec.py -q` → ImportError.

- [ ] **Step 3: Implement**

```python
# src/llm/spec.py
"""LLMCallSpec — what a caller asks for — and its resolution against config and
intelligence classes (spec §3.1)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.config import LLMConfig, normalize_llm_provider
from src.intelligence_classes import IntelligenceClass, resolve_class

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMCallSpec:
    provider: str | None = None  # "anthropic" | "google" | "openai" (legacy ids accepted)
    model: str | None = None  # explicit model id; wins over intelligence_class
    intelligence_class: str | None = None  # e.g. "fast-low"; resolved per provider
    max_tokens: int | None = None
    caller: str = "llm"  # logged; e.g. "playbook:memory-consolidation"


@dataclass(frozen=True)
class ResolvedCall:
    provider: str
    model: str
    base_url: str
    api_key: str
    max_tokens: int
    extras: dict = field(default_factory=dict)  # class slice minus "model"
    caller: str = "llm"

    @property
    def cache_key(self) -> tuple[str, str, str, tuple]:
        return (self.provider, self.model, self.base_url, tuple(sorted(self.extras.items())))


def resolve_call(
    spec: LLMCallSpec,
    config: LLMConfig,
    classes: dict[str, IntelligenceClass],
) -> ResolvedCall:
    """Resolution order: spec.model > intelligence class > config.model > adapter default."""
    provider = normalize_llm_provider(spec.provider or config.provider)
    model = spec.model or ""
    extras: dict = {}

    if not model:
        class_id = spec.intelligence_class or config.default_class
        if class_id:
            cls = classes.get(class_id)
            if cls is None:
                logger.warning("llm: unknown intelligence class %r — falling back", class_id)
            else:
                slice_ = resolve_class(cls, provider)
                if not slice_:
                    logger.warning(
                        "llm: intelligence class %r has no entry for provider %r — falling back",
                        class_id,
                        provider,
                    )
                else:
                    model = str(slice_.pop("model", "") or "")
                    extras = slice_
    if not model:
        model = config.model

    return ResolvedCall(
        provider=provider,
        model=model,
        base_url=config.base_url,
        api_key=config.api_key,
        max_tokens=spec.max_tokens or config.max_tokens,
        extras=extras,
        caller=spec.caller,
    )


def spec_from_llm_config(
    d: dict | None, *, caller: str, max_tokens: int | None = None
) -> LLMCallSpec:
    """Build a spec from a playbook-style ``llm_config`` mapping.  Keys other than
    provider / model / intelligence_class / max_tokens are ignored."""
    d = d or {}
    mt = d.get("max_tokens", max_tokens)
    return LLMCallSpec(
        provider=d.get("provider") or None,
        model=d.get("model") or None,
        intelligence_class=d.get("intelligence_class") or None,
        max_tokens=int(mt) if mt else None,
        caller=caller,
    )
```

- [ ] **Step 4: Run tests** — `pytest tests/llm/test_spec.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/llm/spec.py tests/llm/test_spec.py
git commit -m "feat(llm): LLMCallSpec resolution against config and intelligence classes"
```

---

### Task 4: `LLMClient.complete()` and `LLMLogger.log_llm_call`

**Files:**
- Create: `src/llm/client.py`; fill `src/llm/__init__.py`
- Modify: `src/llm_logger.py` (add `log_llm_call` beside `log_chat_provider_call` at :123)
- Test: `tests/llm/test_client.py`

**Interfaces:**
- Produces: `LLMClient(config: LLMConfig, *, classes_loader: Callable[[], dict[str, IntelligenceClass]], llm_logger: LLMLogger | None = None, provider_factory=create_provider)`, `complete(messages: list[dict] | str, *, system: str = "", spec: LLMCallSpec = LLMCallSpec()) -> LLMResponse`, `LLMResponse(text, tool_calls: list[ToolCall], raw: ChatResponse)`, `ToolCall(id, name, args)`, `is_configured() -> bool`, `is_model_loaded(spec) -> bool`, `LLMClient.with_provider(provider: LLMProvider, config=None) -> LLMClient` (test helper: every resolution returns that provider). `LLMLogger.log_llm_call(*, caller, model, provider, messages, system, tools, max_tokens, response, error, duration_ms)` → `llm.jsonl`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/llm/test_client.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from src.config import LLMConfig
from src.llm import LLMCallSpec, LLMClient
from src.llm.fake import FakeProvider
from src.llm.types import ChatResponse, TextBlock, ToolUseBlock
from src.llm_logger import LLMLogger


def _client(fake: FakeProvider, **cfg) -> LLMClient:
    return LLMClient.with_provider(fake, config=LLMConfig(**cfg))


async def test_complete_string_prompt_returns_text():
    fake = FakeProvider()
    fake.add_text("hello")
    resp = await _client(fake).complete("hi", system="sys")
    assert resp.text == "hello"
    assert resp.tool_calls == []
    call = fake.calls[0]
    assert call.messages == [{"role": "user", "content": "hi"}]
    assert call.system == "sys"
    assert call.max_tokens == 4096  # LLMConfig default


async def test_complete_joins_last_text_parts_and_exposes_tool_calls():
    fake = FakeProvider()
    fake.add_response(
        ChatResponse(content=[TextBlock(text="a"), ToolUseBlock(id="1", name="t", input={"x": 1}), TextBlock(text="b")])
    )
    resp = await _client(fake).complete([{"role": "user", "content": "q"}])
    assert resp.text == "a\nb"
    assert resp.tool_calls[0].name == "t" and resp.tool_calls[0].args == {"x": 1}


async def test_spec_max_tokens_passes_through():
    fake = FakeProvider()
    fake.add_text("x")
    await _client(fake).complete("q", spec=LLMCallSpec(max_tokens=17))
    assert fake.calls[0].max_tokens == 17


async def test_provider_cache_keyed_on_resolution():
    built = []

    def factory(**kw):
        built.append(kw)
        f = FakeProvider(model_name=kw["model"])
        f.add_text("ok")
        f.add_text("ok")
        return f

    client = LLMClient(LLMConfig(provider="anthropic", model="m1"), classes_loader=dict, provider_factory=factory)
    await client.complete("a")
    await client.complete("b")
    await client.complete("c", spec=LLMCallSpec(model="m2"))
    assert [b["model"] for b in built] == ["m1", "m2"]


async def test_logging_writes_llm_jsonl(tmp_path):
    logger = LLMLogger(base_dir=str(tmp_path), enabled=True)
    fake = FakeProvider()
    fake.add_text("logged")
    client = LLMClient.with_provider(fake, config=LLMConfig(), llm_logger=logger)
    await client.complete("q", spec=LLMCallSpec(caller="unit-test"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(str(tmp_path), today, "llm.jsonl")
    entry = json.loads(open(path).read().splitlines()[-1])
    assert entry["caller"] == "unit-test"
    assert entry["provider"] == "FakeProvider"
    assert entry["output"]["text_parts"] == ["logged"]


async def test_error_is_logged_and_reraised(tmp_path):
    logger = LLMLogger(base_dir=str(tmp_path), enabled=True)
    fake = FakeProvider()  # empty queue → RuntimeError
    client = LLMClient.with_provider(fake, config=LLMConfig(), llm_logger=logger)
    with pytest.raises(RuntimeError):
        await client.complete("q")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = json.loads(open(os.path.join(str(tmp_path), today, "llm.jsonl")).read().splitlines()[-1])
    assert "no scripted response" in entry["error"]


def test_is_configured_false_when_factory_fails():
    def boom(**kw):
        raise RuntimeError("no creds")

    client = LLMClient(LLMConfig(), classes_loader=dict, provider_factory=boom)
    assert client.is_configured() is False
    assert _client(FakeProvider()).is_configured() is True
```

- [ ] **Step 2: Run to verify failure** — ImportError on `src.llm.LLMClient`.

- [ ] **Step 3: Implement**

`src/llm_logger.py` — add directly after `log_chat_provider_call` (its body ends at :212):

```python
    def log_llm_call(
        self,
        *,
        caller: str,
        model: str,
        provider: str,
        messages: list[dict],
        system: str,
        tools: list[dict] | None = None,
        max_tokens: int = 1024,
        response: Any = None,
        error: str | None = None,
        duration_ms: int = 0,
    ) -> None:
        """Log one direct-LLM call (``src/llm``) to ``llm.jsonl``."""
        self._log_provider_call(
            filename="llm.jsonl",
            caller=caller, model=model, provider=provider, messages=messages,
            system=system, tools=tools, max_tokens=max_tokens, response=response,
            error=error, duration_ms=duration_ms,
        )
```

and turn the existing body of `log_chat_provider_call` into a private `_log_provider_call(self, *, filename: str, caller, model, provider, messages, system, tools=None, max_tokens=1024, response=None, error=None, duration_ms=0)` whose only change is `self._append(filename, entry)` instead of `self._append("chat_provider.jsonl", entry)`; `log_chat_provider_call` becomes a two-line delegate with `filename="chat_provider.jsonl"` (deleted in Task 14). Also update the class docstring's file list to mention `llm.jsonl`.

`src/llm/client.py`:

```python
"""LLMClient — the direct LLM path: one ``complete()`` and one ``run_tools()``
(spec §3.2).  Owned by the orchestrator; consumers receive it, never build one."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.config import LLMConfig
from src.intelligence_classes import IntelligenceClass
from src.llm.providers import create_provider
from src.llm.providers.base import LLMProvider
from src.llm.spec import LLMCallSpec, ResolvedCall, resolve_call
from src.llm.types import ChatResponse, serialize_canonical
from src.llm_logger import LLMLogger

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: ChatResponse | None = None

    @classmethod
    def from_chat_response(cls, resp: ChatResponse) -> "LLMResponse":
        return cls(
            text="\n".join(resp.text_parts),
            tool_calls=[ToolCall(id=t.id, name=t.name, args=dict(t.input or {})) for t in resp.tool_uses],
            raw=resp,
        )


def _as_messages(messages: list[dict] | str) -> list[dict]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    return list(messages)


class LLMClient:
    def __init__(
        self,
        config: LLMConfig,
        *,
        classes_loader: Callable[[], dict[str, IntelligenceClass]],
        llm_logger: LLMLogger | None = None,
        provider_factory: Callable[..., LLMProvider] = create_provider,
    ):
        self._config = config
        self._classes_loader = classes_loader
        self._logger = llm_logger
        self._factory = provider_factory
        self._providers: dict[tuple, LLMProvider] = {}

    @classmethod
    def with_provider(
        cls,
        provider: LLMProvider,
        *,
        config: LLMConfig | None = None,
        llm_logger: LLMLogger | None = None,
    ) -> "LLMClient":
        """A client whose every resolution yields *provider* (tests, dry runs)."""
        return cls(
            config or LLMConfig(),
            classes_loader=dict,
            llm_logger=llm_logger,
            provider_factory=lambda **_kw: provider,
        )

    @property
    def config(self) -> LLMConfig:
        return self._config

    # -- resolution --------------------------------------------------------

    def resolve(self, spec: LLMCallSpec) -> ResolvedCall:
        return resolve_call(spec, self._config, self._classes_loader())

    def _provider_for(self, resolved: ResolvedCall) -> LLMProvider:
        key = resolved.cache_key
        provider = self._providers.get(key)
        if provider is None:
            provider = self._factory(
                provider=resolved.provider,
                model=resolved.model,
                base_url=resolved.base_url,
                api_key=resolved.api_key,
                extras=dict(resolved.extras),
            )
            self._providers[key] = provider
        return provider

    def is_configured(self, spec: LLMCallSpec = LLMCallSpec()) -> bool:
        try:
            return bool(self._provider_for(self.resolve(spec)).is_configured)
        except Exception as exc:  # missing SDK, missing creds, bad id
            logger.debug("llm: not configured: %s", exc)
            return False

    async def is_model_loaded(self, spec: LLMCallSpec = LLMCallSpec()) -> bool:
        return await self._provider_for(self.resolve(spec)).is_model_loaded()

    # -- calls ---------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict] | str,
        *,
        system: str = "",
        spec: LLMCallSpec = LLMCallSpec(),
    ) -> LLMResponse:
        resolved = self.resolve(spec)
        resp = await self._create_message(
            resolved, messages=_as_messages(messages), system=system, tools=None
        )
        return LLMResponse.from_chat_response(resp)

    async def _create_message(
        self,
        resolved: ResolvedCall,
        *,
        messages: list[dict],
        system: str,
        tools: list[dict] | None,
    ) -> ChatResponse:
        provider = self._provider_for(resolved)
        start = time.monotonic()
        response: ChatResponse | None = None
        error: str | None = None
        try:
            response = await provider.create_message(
                messages=messages, system=system, tools=tools, max_tokens=resolved.max_tokens
            )
            return response
        except Exception as exc:
            error = str(exc)
            raise
        finally:
            if self._logger is not None:
                self._logger.log_llm_call(
                    caller=resolved.caller,
                    model=provider.model_name,
                    provider=type(provider).__name__,
                    messages=serialize_canonical(messages),
                    system=system,
                    tools=tools,
                    max_tokens=resolved.max_tokens,
                    response=response,
                    error=error,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
```

`src/llm/__init__.py`:

```python
"""The direct LLM path.  See docs/superpowers/specs/2026-08-30-llm-direct-path-design.md."""

from src.llm.client import LLMClient, LLMResponse, ToolCall
from src.llm.spec import LLMCallSpec, ResolvedCall, resolve_call, spec_from_llm_config

__all__ = [
    "LLMCallSpec",
    "LLMClient",
    "LLMResponse",
    "ResolvedCall",
    "ToolCall",
    "resolve_call",
    "spec_from_llm_config",
]
```

- [ ] **Step 4: Run tests** — `pytest tests/llm/ tests/test_llm_logger.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/llm src/llm_logger.py tests/llm
git commit -m "feat(llm): LLMClient.complete with provider cache and llm.jsonl logging"
```

---

### Task 5: `LLMClient.run_tools()` — the generic tool loop

**Files:**
- Modify: `src/llm/client.py`, `src/llm/__init__.py` (export `LLMRunResult`)
- Test: `tests/llm/test_run_tools.py`

**Interfaces:**
- Produces: `run_tools(messages, tools: list[dict], execute: Callable[[str, dict], Awaitable[Any]], *, system="", spec=LLMCallSpec(), max_turns=25, on_progress=None, cancel_event=None) -> LLMRunResult`; `LLMRunResult(text, transcript: list[dict], turns: int, stopped_by: str, tool_calls_made: list[str])`. `on_progress(kind, detail)` kinds: `"thinking"`, `"tool_use"`, `"responding"`, `"cancelled"` — the same vocabulary `runner_context._make_supervisor_progress` bridges today.

- [ ] **Step 1: Write the failing tests**

```python
# tests/llm/test_run_tools.py
from __future__ import annotations

import asyncio

from src.config import LLMConfig
from src.llm import LLMClient
from src.llm.fake import FakeProvider

TOOLS = [
    {"name": "list_tasks", "description": "list", "input_schema": {"type": "object", "properties": {}}},
    {"name": "boom", "description": "raises", "input_schema": {"type": "object", "properties": {}}},
]


def _client(fake):
    return LLMClient.with_provider(fake, config=LLMConfig())


async def _exec(name, args):
    if name == "boom":
        raise ValueError("kaboom")
    return {"success": True, "tool": name, "args": args}


async def test_no_tool_calls_returns_text_in_one_turn():
    fake = FakeProvider()
    fake.add_text("done")
    r = await _client(fake).run_tools("go", TOOLS, _exec, system="s")
    assert (r.text, r.turns, r.stopped_by) == ("done", 1, "done")
    assert r.transcript[0] == {"role": "user", "content": "go"}
    assert r.transcript[-1] == {"role": "assistant", "content": "done"}
    assert fake.calls[0].tools == TOOLS


async def test_tool_call_then_text():
    fake = FakeProvider()
    fake.add_tool_call("list_tasks", {"project_id": "p"})
    fake.add_text("finished")
    events = []

    async def progress(kind, detail):
        events.append((kind, detail))

    r = await _client(fake).run_tools("go", TOOLS, _exec, on_progress=progress)
    assert r.text == "finished" and r.turns == 2
    assert r.tool_calls_made == ["list_tasks"]
    # second request carries assistant tool_use + user tool_result
    second = fake.calls[1].messages
    assert second[-2]["role"] == "assistant"
    assert second[-1]["role"] == "user"
    result_block = second[-1]["content"][0]
    assert result_block["type"] == "tool_result"
    assert '"tool": "list_tasks"' in result_block["content"]
    assert [k for k, _ in events] == ["thinking", "tool_use", "thinking", "responding"]


async def test_executor_exception_becomes_error_result_not_abort():
    fake = FakeProvider()
    fake.add_tool_call("boom")
    fake.add_text("recovered")
    r = await _client(fake).run_tools("go", TOOLS, _exec)
    assert r.text == "recovered"
    block = fake.calls[1].messages[-1]["content"][0]
    assert '"success": false' in block["content"] and "kaboom" in block["content"]


async def test_unknown_tool_is_rejected_as_error_result():
    fake = FakeProvider()
    fake.add_tool_call("not_offered")
    fake.add_text("ok")
    r = await _client(fake).run_tools("go", TOOLS, _exec)
    assert r.text == "ok"
    block = fake.calls[1].messages[-1]["content"][0]
    assert "not available" in block["content"]


async def test_max_turns_stops_loop():
    fake = FakeProvider()
    for _ in range(3):
        fake.add_tool_call("list_tasks")
    r = await _client(fake).run_tools("go", TOOLS, _exec, max_turns=2)
    assert r.stopped_by == "max_turns" and r.turns == 2
    assert len(fake.calls) == 2


async def test_cancel_event_stops_before_next_call():
    fake = FakeProvider()
    fake.add_tool_call("list_tasks")
    fake.add_text("never")
    cancel = asyncio.Event()

    async def exec_and_cancel(name, args):
        cancel.set()
        return {"success": True}

    r = await _client(fake).run_tools("go", TOOLS, exec_and_cancel, cancel_event=cancel)
    assert r.stopped_by == "cancelled" and r.text == ""
    assert len(fake.calls) == 1


async def test_multi_tool_turn_executes_all_in_order():
    fake = FakeProvider()
    fake.add_tool_calls([("list_tasks", {"a": 1}), ("list_tasks", {"a": 2})])
    fake.add_text("ok")
    seen = []

    async def ex(name, args):
        seen.append(args["a"])
        return {}

    await _client(fake).run_tools("go", TOOLS, ex)
    assert seen == [1, 2]
```

- [ ] **Step 2: Run to verify failure** — AttributeError `run_tools`.

- [ ] **Step 3: Implement** — add to `src/llm/client.py`:

```python
import asyncio
import json
from collections.abc import Awaitable


@dataclass
class LLMRunResult:
    text: str
    transcript: list[dict]
    turns: int
    stopped_by: str  # "done" | "max_turns" | "cancelled"
    tool_calls_made: list[str] = field(default_factory=list)


ProgressCallback = Callable[[str, str | None], Awaitable[None]]
ToolExecutor = Callable[[str, dict], Awaitable[Any]]


def _json_safe(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return json.dumps({"result": str(obj)})
```

and the method on `LLMClient`:

```python
    async def run_tools(
        self,
        messages: list[dict] | str,
        tools: list[dict],
        execute: ToolExecutor,
        *,
        system: str = "",
        spec: LLMCallSpec = LLMCallSpec(),
        max_turns: int = 25,
        on_progress: ProgressCallback | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> LLMRunResult:
        """Caller-supplied tool loop.  Tool errors become tool results; the loop
        ends when the model answers without tool calls, on ``max_turns``, or on
        ``cancel_event``."""
        resolved = self.resolve(spec)
        transcript = _as_messages(messages)
        offered = {t["name"] for t in tools}
        made: list[str] = []
        turns = 0

        async def _progress(kind: str, detail: str | None = None) -> None:
            if on_progress is not None:
                await on_progress(kind, detail)

        while True:
            if cancel_event is not None and cancel_event.is_set():
                await _progress("cancelled")
                return LLMRunResult("", transcript, turns, "cancelled", made)
            if turns >= max_turns:
                return LLMRunResult("", transcript, turns, "max_turns", made)

            await _progress("thinking", None if turns == 0 else f"round {turns + 1}")
            resp = await self._create_message(
                resolved, messages=transcript, system=system, tools=tools or None
            )
            turns += 1

            if not resp.has_tool_use:
                await _progress("responding")
                text = "\n".join(resp.text_parts).strip()
                transcript.append({"role": "assistant", "content": text})
                return LLMRunResult(text, transcript, turns, "done", made)

            transcript.append({"role": "assistant", "content": resp.tool_uses})
            results: list[dict] = []
            for call in resp.tool_uses:
                await _progress("tool_use", call.name)
                made.append(call.name)
                if call.name not in offered:
                    result: Any = {
                        "success": False,
                        "error": f"Tool '{call.name}' is not available in this call",
                    }
                else:
                    try:
                        result = await execute(call.name, dict(call.input or {}))
                    except Exception as exc:
                        logger.warning("llm.run_tools: tool %s raised: %s", call.name, exc)
                        result = {"success": False, "error": str(exc)}
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": _json_safe(result)}
                )
            transcript.append({"role": "user", "content": results})
```

Export `LLMRunResult` from `src/llm/__init__.py`.

- [ ] **Step 4: Run tests** — `pytest tests/llm/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/llm tests/llm/test_run_tools.py
git commit -m "feat(llm): run_tools — generic caller-supplied tool loop"
```

---

### Task 6: The orchestrator owns one `LLMClient`

**Files:**
- Modify: `src/orchestrator/core.py` (`__init__` after :469; the class-reload block :2299-2305)
- Test: `tests/test_orchestrator_llm.py`

**Interfaces:**
- Produces: `Orchestrator.llm: LLMClient` (built from `config.llm`, the intelligence-class loader, `self.llm_logger`). Later tasks consume `orchestrator.llm`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orchestrator_llm.py
from src.config import AppConfig, DiscordConfig, LLMConfig
from src.llm import LLMCallSpec, LLMClient
from src.orchestrator import Orchestrator


def test_orchestrator_builds_llm_client(tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database_path=str(tmp_path / "t.db"),
        data_dir=str(tmp_path / "d"),
        llm=LLMConfig(provider="google", model="gemini-2.5-flash"),
    )
    orch = Orchestrator(cfg)
    assert isinstance(orch.llm, LLMClient)
    assert orch.llm.config.provider == "google"
    assert orch.llm.resolve(LLMCallSpec()).model == "gemini-2.5-flash"
```

- [ ] **Step 2: Run to verify failure** — AttributeError `llm`.

- [ ] **Step 3: Implement** — in `core.py` right after the `SessionSpecBuilder(...)` construction (ends :469):

```python
        from src.llm import LLMClient

        self.llm = LLMClient(
            config.llm,
            classes_loader=lambda: load_intelligence_classes(self.config.data_dir),
            llm_logger=self.llm_logger if self.llm_logger._enabled else None,
        )
```

(`self.llm_logger` is created at :378, before this point.) Because the loader is a callable, the reload at :2299-2305 needs no change — classes are re-read per call.

- [ ] **Step 4: Run tests** — `pytest tests/test_orchestrator_llm.py tests/test_orchestrator.py -q` → PASS.

- [ ] **Step 5: Commit (Phase 1 boundary — run the full suite first; expected green)**

```bash
git add src/orchestrator/core.py tests/test_orchestrator_llm.py
git commit -m "feat(orchestrator): own a single LLMClient built from config.llm"
```

---

# Phase 2 — port consumers (spec §5)

### Task 7: Plugin `invoke_llm` on the client

**Files:**
- Modify: `src/plugins/base.py:492-535`, `src/orchestrator/core.py:627-672` (the callback moves out of `set_supervisor` into a method wired in `__init__`), `src/plugins/registry.py` (no change needed — callback signature is opaque)
- Test: `tests/test_plugins.py:1054-1110` (rewrite the two `invoke_llm` tests), new `tests/test_plugin_invoke_llm.py`

**Interfaces:**
- Produces: `PluginContext.invoke_llm(prompt, *, intelligence_class=None, model=None, provider=None, tools=None, system="") -> str`; `Orchestrator._plugin_invoke_llm(prompt, plugin_name, *, intelligence_class, model, provider, tools, system) -> str`; `Orchestrator.set_command_handler` now also wires `plugin_registry.set_execute_command_callback(handler.execute)` and `set_active_project_id_getter(lambda: handler._active_project_id)` (moved from `set_supervisor`).

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_plugins.py::TestPluginContextInvokeLLM` (both tests) with:

```python
    async def test_invoke_llm_calls_callback(self, tmp_path):
        callback = AsyncMock(return_value="LLM response")
        ctx = PluginContext(
            plugin_name="test", install_path=str(tmp_path), db=AsyncMock(), bus=MagicMock(),
            command_registry={}, tool_registry={}, event_type_registry=set(),
            invoke_llm_callback=callback,
        )
        assert await ctx.invoke_llm("What is 2+2?") == "LLM response"
        callback.assert_called_once_with(
            "What is 2+2?", "test", intelligence_class=None, model=None, provider=None,
            tools=None, system="",
        )

    async def test_invoke_llm_passes_overrides(self, tmp_path):
        callback = AsyncMock(return_value="ok")
        ctx = PluginContext(
            plugin_name="test", install_path=str(tmp_path), db=AsyncMock(), bus=MagicMock(),
            command_registry={}, tool_registry={}, event_type_registry=set(),
            invoke_llm_callback=callback,
        )
        tools = [{"name": "t", "input_schema": {"type": "object"}}]
        await ctx.invoke_llm("p", intelligence_class="fast-low", model="m", provider="google",
                             tools=tools, system="s")
        callback.assert_called_once_with(
            "p", "test", intelligence_class="fast-low", model="m", provider="google",
            tools=tools, system="s",
        )
```

New `tests/test_plugin_invoke_llm.py`:

```python
"""Orchestrator._plugin_invoke_llm routes to LLMClient.complete / run_tools."""

from __future__ import annotations

from unittest.mock import AsyncMock

from src.config import AppConfig, DiscordConfig
from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.orchestrator import Orchestrator


def _orch(tmp_path, fake):
    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "w"), database_path=str(tmp_path / "t.db"),
                    data_dir=str(tmp_path / "d"))
    o = Orchestrator(cfg)
    o.llm = LLMClient.with_provider(fake)
    return o


async def test_plain_prompt_uses_complete(tmp_path):
    fake = FakeProvider()
    fake.add_text("4")
    o = _orch(tmp_path, fake)
    assert await o._plugin_invoke_llm("2+2?", "calc") == "4"
    assert fake.calls[0].tools is None
    assert "plugin:calc" in fake.calls[0].system


async def test_tools_use_run_tools_with_handler_execute(tmp_path):
    fake = FakeProvider()
    fake.add_tool_call("list_tasks", {"x": 1})
    fake.add_text("done")
    o = _orch(tmp_path, fake)
    handler = AsyncMock()
    handler.execute = AsyncMock(return_value={"success": True})
    o._command_handler = handler
    tools = [{"name": "list_tasks", "input_schema": {"type": "object", "properties": {}}}]
    assert await o._plugin_invoke_llm("go", "p", tools=tools) == "done"
    handler.execute.assert_awaited_once_with("list_tasks", {"x": 1})
```

- [ ] **Step 2: Run to verify failure** — `pytest tests/test_plugins.py -k invoke_llm tests/test_plugin_invoke_llm.py -q` → assertion / AttributeError.

- [ ] **Step 3: Implement**

`src/plugins/base.py:492-535` becomes:

```python
    async def invoke_llm(
        self,
        prompt: str,
        *,
        intelligence_class: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        tools: list[dict] | None = None,
        system: str = "",
    ) -> str:
        """One direct LLM call on the daemon's ``llm`` client (spec: llm-direct-path §5).

        Without ``tools`` this is a single completion; with ``tools`` (JSON-schema
        tool definitions naming CommandHandler commands) it runs a tool loop where
        every call executes through ``execute_command``.  ``intelligence_class``
        picks a tier from ``vault/intelligence-classes``; ``model``/``provider``
        override it.  For heavy autonomous work create a task instead.
        """
        if not self._invoke_llm_callback:
            raise RuntimeError("LLM invocation not available")
        return await self._invoke_llm_callback(
            prompt,
            self._plugin_name,
            intelligence_class=intelligence_class,
            model=model,
            provider=provider,
            tools=tools,
            system=system,
        )
```

Update the docstring example at `plugins/base.py:719` to the new keyword names.

`src/orchestrator/core.py`: delete lines 627-672 (the nested `_plugin_invoke_llm` and its `set_invoke_llm_callback`) from `set_supervisor`, and delete lines 674-679 (the active-project getter / execute callback wiring) from it too. Add a method to `Orchestrator`:

```python
    async def _plugin_invoke_llm(
        self,
        prompt: str,
        plugin_name: str,
        *,
        intelligence_class: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        tools: list[dict] | None = None,
        system: str = "",
    ) -> str:
        from src.llm import LLMCallSpec

        spec = LLMCallSpec(
            provider=provider,
            model=model,
            intelligence_class=intelligence_class,
            caller=f"plugin:{plugin_name}",
        )
        system_prompt = system or f"You are a helper for plugin:{plugin_name}."
        if not tools:
            resp = await self.llm.complete(prompt, system=system_prompt, spec=spec)
            return resp.text
        handler = self._command_handler
        if handler is None:
            raise RuntimeError("invoke_llm with tools needs the command handler wired")
        result = await self.llm.run_tools(
            prompt, tools, handler.execute, system=system_prompt, spec=spec
        )
        return result.text
```

In `__init__`, right after `self.llm = LLMClient(...)` (Task 6): nothing — the registry may not exist yet. Instead, find where `self.plugin_registry` is constructed in `initialize()` (grep `self.plugin_registry = PluginRegistry(`) and immediately after it add:

```python
        self.plugin_registry.set_invoke_llm_callback(self._plugin_invoke_llm)
```

Extend `set_command_handler` (:594-608) with, after the playbook-manager forward:

```python
        plugin_registry = getattr(self, "plugin_registry", None)
        if plugin_registry is not None:
            plugin_registry.set_active_project_id_getter(lambda: handler._active_project_id)
            plugin_registry.set_execute_command_callback(handler.execute)
```

and, in `initialize()` right after the `set_invoke_llm_callback` line, re-apply the handler wiring if a handler was set before the registry existed:

```python
        if self._command_handler is not None:
            self.set_command_handler(self._command_handler)
```

- [ ] **Step 4: Run tests** — `pytest tests/test_plugins.py tests/test_plugin_invoke_llm.py tests/test_supervisor_cutover.py -q`. `test_supervisor_cutover.py:797-830` ("Plugin invoke_llm fallback … delegates to supervisor.chat") now fails by design: **delete that test class** — its contract is replaced by `tests/test_plugin_invoke_llm.py`. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/plugins/base.py src/orchestrator/core.py tests/test_plugins.py tests/test_plugin_invoke_llm.py tests/test_supervisor_cutover.py
git commit -m "feat(plugins): invoke_llm runs on the LLMClient with intelligence classes"
```

---

### Task 8: Reference-stub enricher on the client

**Files:**
- Modify: `src/reference_stub_enricher.py:56, 583-608, 973-1034`; `src/config.py` (`MemoryConfig` :361-365 + loader :2300-2317); `src/orchestrator/core.py:1793-1800`
- Test: `tests/test_reference_stub_enricher.py` (fixture at :110-114 and the two constructions at :520, :549)

**Interfaces:**
- Produces: `ReferenceStubEnricher(bus, vault_projects_dir, config: MemoryConfig, *, llm: LLMClient | None = None, enabled=True, max_source_chars=..., max_tokens=2048)`; `MemoryConfig.stub_enrichment_class: str = ""` (replaces `stub_enrichment_provider` / `stub_enrichment_model`), loaded from YAML.

- [ ] **Step 1: Update the tests to the new constructor**

In `tests/test_reference_stub_enricher.py`, wherever a `fake_provider` (an object with `create_message`) is passed as `provider=`, wrap it: `llm=LLMClient.with_provider(fake_provider)` with `from src.llm import LLMClient`; ensure the fake subclasses `src.llm.providers.base.LLMProvider` (or replace it with `src.llm.fake.FakeProvider` and `add_text(...)` the scripted enrichment JSON). `provider=None` at :524 becomes `llm=None`. Add one test:

```python
async def test_enrichment_uses_configured_class(tmp_path):
    from src.config import LLMConfig, MemoryConfig
    from src.llm import LLMClient
    from src.llm.fake import FakeProvider
    from src.reference_stub_enricher import ReferenceStubEnricher

    fake = FakeProvider()
    fake.add_text('{"summary": "s", "key_decisions": "d", "key_interfaces": "i"}')
    seen = {}

    class SpyClient(LLMClient):
        async def complete(self, messages, *, system="", spec=None):
            seen["spec"] = spec
            return await super().complete(messages, system=system, spec=spec)

    client = SpyClient.with_provider(fake, config=LLMConfig())
    enricher = ReferenceStubEnricher(
        bus=AsyncMock(), vault_projects_dir=str(tmp_path),
        config=MemoryConfig(stub_enrichment_class="fast-low"), llm=client,
    )
    out = await enricher._summarize_document("some content")
    assert out["summary"] == "s"
    assert seen["spec"].intelligence_class == "fast-low"
    assert seen["spec"].caller == "stub-enricher"
```

(Confirm `_summarize_document`'s exact name/signature at :1002 and adjust the call.)

- [ ] **Step 2: Run to verify failure** — TypeError on `llm=`.

- [ ] **Step 3: Implement**

`src/config.py` `MemoryConfig` :363-364: replace the two fields with

```python
    stub_enrichment_class: str = ""  # intelligence class for enrichment calls (empty = llm.default_class)
```

and in the memory loader (:2300-2317) add `stub_enrichment_enabled=mem.get("stub_enrichment_enabled", True), stub_enrichment_class=str(mem.get("stub_enrichment_class", "") or ""), stub_enrichment_max_source_chars=int(mem.get("stub_enrichment_max_source_chars", 20_000)),`.

`src/reference_stub_enricher.py`: the TYPE_CHECKING import at :56 becomes `from src.llm import LLMClient`; constructor parameter `provider: ChatProvider | None = None` → `llm: "LLMClient | None" = None`, stored as `self._llm = llm`. Delete `_get_provider` (:973-1000). The call at :1023-1027 becomes:

```python
        from src.llm import LLMCallSpec

        if self._llm is None:
            logger.warning("ReferenceStubEnricher: no LLM client — skipping enrichment")
            return {"summary": "", "key_decisions": "", "key_interfaces": ""}
        response = await self._llm.complete(
            [{"role": "user", "content": user_prompt}],
            system=SUMMARIZE_SYSTEM_PROMPT,
            spec=LLMCallSpec(
                intelligence_class=self._config.stub_enrichment_class or None,
                max_tokens=self._max_tokens,
                caller="stub-enricher",
            ),
        )
        response_text = response.text
        if not response_text:
            return {"summary": "", "key_decisions": "", "key_interfaces": ""}
        return parse_enrichment_response(response_text)
```

Replace any other `self._get_provider()` call (e.g. :799) with a `self._llm is None` check. `src/orchestrator/core.py:1793-1800`: pass `llm=self.llm` and `max_tokens=self.config.llm.max_tokens`.

- [ ] **Step 4: Run tests** — `pytest tests/test_reference_stub_enricher.py tests/llm/test_config.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/reference_stub_enricher.py src/config.py src/orchestrator/core.py tests/test_reference_stub_enricher.py
git commit -m "feat(memory): stub enricher calls the LLMClient with memory.stub_enrichment_class"
```

---

### Task 9: `aq vault rebuild-index --with-summaries` on the client

**Files:**
- Modify: `src/commands/system_commands.py:1039-1053`, `src/vault_index.py:148-267`
- Test: `tests/test_vault_index_summaries.py`

**Interfaces:**
- Produces: `VaultIndexGenerator.generate_all_with_summaries(llm: LLMClient) -> list[str]`; `_generate_summary(abs_dir, llm) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_vault_index_summaries.py
from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.vault_index import VaultIndexGenerator


async def test_summary_uses_llm_complete(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "a.md").write_text("# A\nsome knowledge\n")
    fake = FakeProvider()
    fake.add_text('"Covers A."')
    gen = VaultIndexGenerator(str(tmp_path))
    text = await gen._generate_summary(str(tmp_path / "memory"), LLMClient.with_provider(fake))
    assert text == "Covers A."
    assert "concise technical writer" in fake.calls[0].system


async def test_summary_failure_returns_empty(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "a.md").write_text("x")
    gen = VaultIndexGenerator(str(tmp_path))
    assert await gen._generate_summary(str(tmp_path / "memory"), LLMClient.with_provider(FakeProvider())) == ""
```

- [ ] **Step 2: Run to verify failure** — `_generate_summary` calls `.create_message` on the client → AttributeError.

- [ ] **Step 3: Implement** — `vault_index.py:252-265`:

```python
        try:
            from src.llm import LLMCallSpec

            resp = await llm.complete(
                [{"role": "user", "content": user_msg}],
                system=(
                    "You are a concise technical writer. Respond with only the "
                    "requested sentence, nothing else."
                ),
                spec=LLMCallSpec(max_tokens=1024, caller="vault-index"),
            )
            text = resp.text.strip().splitlines()[-1].strip().strip('"').strip("'") if resp.text.strip() else ""
            if text and len(text) < 200:
                return text
        except Exception:
            logger.debug("Summary generation failed for %s", abs_dir, exc_info=True)
        return ""
```

Rename the parameter `chat_provider` → `llm` in both methods (:148, :205). `system_commands.py:1039-1053` becomes:

```python
        if with_summaries:
            llm = getattr(self.orchestrator, "llm", None)
            if llm is None or not llm.is_configured():
                return {"error": "LLM is not configured (config.llm) — cannot generate summaries."}
            written = await gen.generate_all_with_summaries(llm)
```

- [ ] **Step 4: Run tests** — `pytest tests/test_vault_index_summaries.py tests/test_vault_index.py tests/test_system_commands.py -q` (skip files that don't exist) → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vault_index.py src/commands/system_commands.py tests/test_vault_index_summaries.py
git commit -m "feat(vault): rebuild-index summaries use the LLMClient"
```

---

### Task 10: Playbook runner — `PlaybookServices`, node execution via `run_tools`, transitions via `complete`

**Files:**
- Create: `src/playbooks/services.py`
- Modify: `src/playbooks/runner.py` (:64 import, :109-118 `_DummySupervisor`, :146-216 `__init__`, :1366-1373, :1710-1794 `_execute_single_node`, :1796-1859 `_execute_node_via_platform`, :2242-2251 `dry_run`), `src/playbooks/runner_transitions.py:619-626`, `src/playbooks/runner_context.py:242-297, 385-403, 405-435`, `src/playbooks/compiler.py:370-375`
- Test: `tests/test_playbook_runner.py` (fixture :48-59 + the ~240 `supervisor.chat` references), `tests/test_playbook_services.py`

**Interfaces:**
- Produces: `PlaybookServices(llm: LLMClient, handler: CommandHandler, tool_registry: ToolRegistry, llm_logger: LLMLogger | None = None, runtimes: Any = None)` with `@classmethod for_tests(llm) -> PlaybookServices` (handler/tool_registry are `MagicMock`/`AsyncMock`); `PlaybookRunner(graph, event, services: PlaybookServices, db=None, on_progress=None, max_daily_playbook_tokens=None, daily_token_tracker=None, daily_token_cap=None, event_bus=None)` — the `supervisor` and `runtimes` parameters are gone (`runtimes` lives on services); `PlaybookRunner.services`; `PlaybookRunner._last_transcript: list[dict]`.
- Consumes: `LLMClient.run_tools/complete`, `spec_from_llm_config`, `LLMRunResult`.

- [ ] **Step 1: Write `services.py` and its test**

```python
# src/playbooks/services.py
"""What a playbook run needs from the daemon, bundled (llm-direct-path §5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from src.commands.handler import CommandHandler
    from src.llm import LLMClient
    from src.llm_logger import LLMLogger
    from src.tools.registry import ToolRegistry

#: Navigation tools the old chat loop special-cased; a playbook node never gets them.
_EXCLUDED_TOOLS = frozenset({"load_tools", "reply_to_user"})


@dataclass
class PlaybookServices:
    llm: "LLMClient"
    handler: "CommandHandler"
    tool_registry: "ToolRegistry"
    llm_logger: "LLMLogger | None" = None
    runtimes: Any = None  # RuntimeRegistry for harness-less one-shot node sessions

    def node_tools(self, allowed: list[str] | None) -> list[dict]:
        """Tool definitions for one node: exactly ``allowed`` (validated against the
        registry) or the registry's core set when the profile lists none."""
        if allowed is None:
            tools = self.tool_registry.get_core_tools()
        else:
            known = {t["name"]: t for t in self.tool_registry.get_all_tools()}
            unknown = sorted(set(allowed) - set(known))
            if unknown:
                raise ValueError(f"Unknown tool names in profile allowed_tools: {unknown}")
            tools = [known[n] for n in allowed]
        return [t for t in tools if t["name"] not in _EXCLUDED_TOOLS]

    @classmethod
    def for_tests(cls, llm: "LLMClient") -> "PlaybookServices":
        registry = MagicMock()
        registry.get_core_tools.return_value = []
        registry.get_all_tools.return_value = []
        handler = MagicMock()
        handler.execute = AsyncMock(return_value={"success": True})
        return cls(llm=llm, handler=handler, tool_registry=registry)
```

```python
# tests/test_playbook_services.py
import pytest

from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.playbooks.services import PlaybookServices

T = lambda n: {"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}}  # noqa: E731


def _svc(all_tools, core):
    s = PlaybookServices.for_tests(LLMClient.with_provider(FakeProvider()))
    s.tool_registry.get_all_tools.return_value = all_tools
    s.tool_registry.get_core_tools.return_value = core
    return s


def test_allowed_filters_and_orders_and_strips_navigation():
    s = _svc([T("a"), T("b"), T("reply_to_user"), T("load_tools")], [])
    assert [t["name"] for t in s.node_tools(["b", "reply_to_user", "a"])] == ["b", "a"]


def test_none_uses_core_tools():
    s = _svc([], [T("core"), T("load_tools")])
    assert [t["name"] for t in s.node_tools(None)] == ["core"]


def test_unknown_allowed_raises():
    with pytest.raises(ValueError, match="Unknown tool names"):
        _svc([T("a")], []).node_tools(["zzz"])


def test_empty_allowed_is_no_tools():
    assert _svc([T("a")], [T("a")]).node_tools([]) == []
```

Run: `pytest tests/test_playbook_services.py -q` → PASS (this task's first commit-able unit).

- [ ] **Step 2: Rewrite the runner fixture (tests fail until Step 3)**

In `tests/test_playbook_runner.py` replace the `mock_supervisor` fixture (:48-59) with:

```python
from types import SimpleNamespace

from src.llm import LLMClient, LLMRunResult
from src.llm.fake import FakeProvider
from src.playbooks.services import PlaybookServices


def _run_result(text: str, transcript: list[dict] | None = None) -> LLMRunResult:
    return LLMRunResult(text=text, transcript=transcript or [], turns=1, stopped_by="done")


@pytest.fixture
def mock_services():
    """PlaybookServices whose llm.run_tools / llm.complete are AsyncMocks.

    ``services.llm.run_tools`` returns ``_run_result("Done.")``; tests set
    ``services.llm.run_tools.return_value = _run_result("...")`` or
    ``.side_effect = [...]`` exactly where they used ``mock_supervisor.chat``.
    """
    services = PlaybookServices.for_tests(LLMClient.with_provider(FakeProvider()))
    services.llm = MagicMock()
    services.llm.config = SimpleNamespace(max_tokens=2048)
    services.llm.run_tools = AsyncMock(return_value=_run_result("Done."))
    services.llm.complete = AsyncMock(return_value=SimpleNamespace(text="1", tool_calls=[]))
    return services
```

Then mechanically:

```bash
sed -i 's/mock_supervisor\.chat\.return_value = \("[^"]*"\)/mock_supervisor.llm.run_tools.return_value = _run_result(\1)/g; s/mock_supervisor\.chat\.side_effect = \[/mock_supervisor.llm.run_tools.side_effect = [/g; s/mock_supervisor\.chat/mock_supervisor.llm.run_tools/g; s/\bmock_supervisor\b/mock_services/g; s/supervisor=mock_services/services=mock_services/g; s/supervisor=_DummySupervisor()/services=mock_services/g' tests/test_playbook_runner.py
```

Hand-fix what sed cannot: (a) `side_effect = [...]` lists of strings → wrap each string in `_run_result(...)`; (b) assertions on `call_args.kwargs["text"]` → `call_args.args[0]` (the prompt is the first positional arg to `run_tools`); `kwargs["llm_config"]` → `kwargs["spec"]` compared with `LLMCallSpec(...)` fields; `kwargs["history"]` → `call_args.args[0]` contains the context text (context is folded into the prompt, see Step 3); `kwargs["tool_overrides"]` → `call_args.args[1]` (the tools list); (c) transition tests that asserted `supervisor.chat(... tool_overrides=[])` now assert `mock_services.llm.complete` was awaited and inspect its `return_value.text`; (d) `_StubSupervisor` (:8641-8651) → construct the context mixin with `_last_transcript=last_messages` instead of `supervisor`; (e) any test constructing `PlaybookRunner(...)` with `runtimes=` moves it to `mock_services.runtimes = ...`.

- [ ] **Step 3: Implement the runner changes**

`runner.py:64`: replace `from src.runtimes.supervisor import Supervisor` with `from src.playbooks.services import PlaybookServices`. Delete `_DummySupervisor` (:109-118). `__init__` (:146-216): parameter `supervisor: Supervisor` → `services: PlaybookServices`; drop the `runtimes: Any = None` parameter; body: `self.services = services`, `self._runtimes = services.runtimes`, add `self._last_transcript: list[dict] = []`. The two `_DummySupervisor()` call sites (:1369, :2245) pass `services` — `handle_timeout` takes a `services: PlaybookServices | None` (it currently takes `supervisor`; rename) and uses `services` (no fallback needed: `run_tools` is never reached on the no-timeout-node path); `dry_run` builds `PlaybookServices.for_tests(LLMClient.with_provider(FakeProvider()))`.

Add to the runner:

```python
    def _build_node_system_prompt(self) -> str:
        parts = [
            "You are executing one step of an Agent Queue playbook. Use the tools you are "
            "given to read and change system state. When the step is finished, answer in "
            "plain text without further tool calls."
        ]
        if self._profile is not None and self._profile.system_prompt_suffix:
            parts.append(self._profile.system_prompt_suffix)
        project_id = self.event.get("project_id")
        if project_id:
            parts.append(f"Active project: {project_id}")
        return "\n\n".join(parts)
```

`_execute_single_node` (:1710-1794): replace lines 1724-1772 with

```python
        from src.llm import spec_from_llm_config

        node_spec = spec_from_llm_config(
            self._resolve_node_llm_config(node),
            caller=f"playbook:{self._playbook_id}:{node_id}",
            max_tokens=self.services.llm.config.max_tokens,
        )
        supervisor_progress = self._make_supervisor_progress(node_id)
        context = self._build_node_context()
        timeout = node.get("timeout_seconds")

        handler = self.services.handler
        caller_pid = self._profile.id if self._profile is not None else None
        handler.set_caller_profile(caller_pid)
        project_id = self.event.get("project_id")
        if project_id:
            handler.set_active_project(project_id)
        try:
            if self._profile is not None and getattr(self._profile, "harness", ""):
                response = await self._execute_node_via_platform(
                    node_id=node_id, prompt=prompt, timeout=timeout,
                    on_progress=supervisor_progress,
                )
            else:
                coro = self.services.llm.run_tools(
                    _fold_context(context, prompt),
                    self.services.node_tools(self._tool_overrides),
                    handler.execute,
                    system=self._build_node_system_prompt(),
                    spec=node_spec,
                    on_progress=supervisor_progress,
                )
                run = await (asyncio.wait_for(coro, timeout=timeout) if timeout else coro)
                self._last_transcript = run.transcript
                response = run.text
        except asyncio.TimeoutError:
            trace_entry.status = "failed"
            raise TimeoutError(f"Node '{node_id}' timed out after {timeout}s") from None
        finally:
            handler.set_caller_profile(None)
```

with a module-level helper:

```python
def _fold_context(context: list[dict], prompt: str) -> list[dict]:
    """Per-node context (prior node summaries) plus the node prompt as one message list."""
    messages = [dict(m) for m in context]
    if messages and messages[-1].get("role") == "user":
        messages[-1]["content"] = f"{messages[-1]['content']}\n{prompt}"
    else:
        messages.append({"role": "user", "content": prompt})
    return messages
```

`_execute_node_via_platform` (:1796-1859): the runtime-registry dispatch stays but keyed on the harness: replace `self._profile.runtime` in the error message with `self._profile.harness`, and `llm_logger=getattr(self.supervisor, "_llm_logger", None)` → `llm_logger=self.services.llm_logger`. `_make_supervisor_progress` is unchanged.

`runner_transitions.py:619-626`:

```python
        from src.llm import spec_from_llm_config

        decision = (
            await self.services.llm.complete(
                list(self.messages) + [{"role": "user", "content": transition_prompt}],
                system="You classify which condition matched. Reply with only a number.",
                spec=spec_from_llm_config(
                    transition_llm_config,
                    caller=f"playbook:{self._playbook_id}:transition:{node_id}",
                ),
            )
        ).text
```

`runner_context.py:287`: `last_messages = getattr(self.supervisor, "_last_messages", None) or []` → `last_messages = self._last_transcript`. Update the `_resolve_node_llm_config` docstring (:389) to say "use the client's default". Declare `_last_transcript: list[dict]` and `services: "PlaybookServices"` on the mixin's attribute block (near :163 of `runner_transitions.py` and the equivalent in `runner_context.py`).

`compiler.py:370-375`: after copying `llm_config` / `transition_llm_config`, warn on dropped keys:

```python
        for key in ("llm_config", "transition_llm_config"):
            cfg = result.get(key)
            if isinstance(cfg, dict):
                dropped = sorted(set(cfg) - {"provider", "model", "intelligence_class", "max_tokens"})
                if dropped:
                    logger.warning("playbook %s: %s keys %s are ignored", result.get("id"), key, dropped)
```

- [ ] **Step 4: Run tests** — `pytest tests/test_playbook_runner.py tests/test_playbook_services.py -q`, iterate on the hand-fixes from Step 2 until green. Then `pytest tests/ -k "playbook" -q -n auto` — construction sites still break (Task 11) only where they import `Supervisor`; those files are next.

- [ ] **Step 5: Commit**

```bash
git add src/playbooks tests/test_playbook_runner.py tests/test_playbook_services.py
git commit -m "feat(playbooks): runner takes PlaybookServices; nodes run on llm.run_tools, transitions on llm.complete"
```

---

### Task 11: Playbook construction sites stop building a Supervisor

**Files:**
- Modify: `src/orchestrator/core.py` (add `playbook_services()`; `_on_playbook_trigger` :1119-1140), `src/commands/playbook_commands.py:380-392, 450-460, 902-932, 1796-1812`, `src/playbooks/resume_handler.py:240-249`, `src/workflow_stage_resume_handler.py:266-275`
- Test: `tests/test_playbook_resume_handler.py`, `tests/test_playbook_handler.py`, `tests/test_reflection_e2e.py`, `tests/test_workflow_stage_resume*.py` — replace `Supervisor` patches with `orchestrator.playbook_services` / `mock_services`.

**Interfaces:**
- Produces: `Orchestrator.playbook_services() -> PlaybookServices` (llm=self.llm, handler=self._command_handler, tool_registry=self._tool_registry, llm_logger=self.llm_logger, runtimes=self._runtimes). Raises `RuntimeError("command handler not wired")` if `_command_handler` is None.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_playbook_services.py (append)
def test_orchestrator_playbook_services(tmp_path):
    from unittest.mock import MagicMock
    from src.config import AppConfig, DiscordConfig
    from src.orchestrator import Orchestrator

    cfg = AppConfig(discord=DiscordConfig(bot_token="t", guild_id="1"),
                    workspace_dir=str(tmp_path / "w"), database_path=str(tmp_path / "t.db"),
                    data_dir=str(tmp_path / "d"))
    o = Orchestrator(cfg)
    with pytest.raises(RuntimeError, match="command handler"):
        o.playbook_services()
    o._command_handler = MagicMock()
    o._tool_registry = MagicMock()
    s = o.playbook_services()
    assert s.llm is o.llm and s.handler is o._command_handler and s.tool_registry is o._tool_registry
```

- [ ] **Step 2: Run to verify failure** — AttributeError `playbook_services`.

- [ ] **Step 3: Implement**

`core.py` — add near `set_command_handler`:

```python
    def playbook_services(self):
        """Everything a PlaybookRunner needs, built from daemon-wide singletons."""
        from src.playbooks.services import PlaybookServices

        if self._command_handler is None:
            raise RuntimeError("playbook_services: command handler not wired yet")
        return PlaybookServices(
            llm=self.llm,
            handler=self._command_handler,
            tool_registry=self._tool_registry,
            llm_logger=self.llm_logger,
            runtimes=self._runtimes,
        )
```

(`self._tool_registry` must exist from `__init__`: add `self._tool_registry = None` next to `self._command_handler = None` if it is only set in `set_supervisor`; Task 12 gives it a real value.)

`_on_playbook_trigger` (:1119-1140): replace the Supervisor construction, `initialize`, `set_active_project`, and `set_plugin_registry` lines with

```python
            try:
                services = self.playbook_services()
            except RuntimeError as exc:
                logger.error("Playbook trigger for '%s': %s — skipping", playbook.id, exc)
                return
            if not services.llm.is_configured():
                logger.error(
                    "Playbook trigger for '%s': LLM not configured (config.llm) — skipping",
                    playbook.id,
                )
                return
```

and pass `services=services` to `PlaybookRunner(...)` instead of the supervisor (drop any `runtimes=` kwarg there). Remove the now-unused `Supervisor` import at :909-911.

`playbook_commands.py`: at :380-392 and :1796-1812 replace the `supervisor = SupervisorCls(...)` / `initialize()` blocks with `services = self.orchestrator.playbook_services()` guarded by the same `if on_timeout_node:` condition (`services = None` otherwise), and pass `services` where `supervisor` was passed to `PlaybookRunner.handle_timeout(...)`. At :450-460 and :902-932: `services = self.orchestrator.playbook_services()`; `if not services.llm.is_configured(): return {"error": "LLM is not configured (config.llm)"}`; pass `services=services` to the runner; delete `set_active_project`, `set_plugin_registry`, `runtimes=` lines. `resume_handler.py:240-249` and `workflow_stage_resume_handler.py:266-275`: same shape, logging instead of returning a dict.

- [ ] **Step 4: Run tests** — `pytest tests/ -k "playbook or reflection_e2e or workflow_stage" -q -n auto`; fix the test patches (`patch("src.runtimes.supervisor.Supervisor")` → set `orchestrator.playbook_services = lambda: mock_services`). Then the full suite — Phase 2 boundary, expected green (the Supervisor still exists; nothing in the playbook path uses it).

- [ ] **Step 5: Commit**

```bash
git add src/orchestrator/core.py src/commands/playbook_commands.py src/playbooks/resume_handler.py src/workflow_stage_resume_handler.py tests/
git commit -m "refactor(playbooks): construction sites use orchestrator.playbook_services(), no Supervisor"
```

---

# Phase 3 — delete (spec §6)

### Task 12: Delete the in-process Supervisor; `harness` is the only selector

**Files:**
- Delete: `src/runtimes/supervisor.py`, `src/chat_agent.py`, `src/chat_observer.py`, `src/reflection.py`, `tests/test_supervisor.py`, `tests/test_supervisor_model_override.py`, `tests/test_llm_config_provider_swap.py`, `tests/test_supervisor_profile_config.py`, `tests/test_supervisor_observe.py`, `tests/test_supervisor_runtime.py`, `tests/test_supervisor_cutover.py` (keep only its Discord-routing tests: move `TestDiscordRouting*` classes into `tests/test_discord_supervisor_routing.py` first)
- Modify: `src/main.py:36, 91-123`, `src/runtimes/__init__.py` (`default_registry`), `src/orchestrator/core.py:415, 614-626` (`set_supervisor`), `src/orchestrator/execution.py:352-357`, `src/orchestrator/agent_reconciler.py:295-307`, `src/messaging/port.py:31`, `src/config.py` (`default_runtime` :1438-1443 + :1632-1648; `SupervisorConfig` :567-582 + loader :2214-2236; `ReflectionConfig`/`ObservationConfig` classes), `src/profiles/parser.py:533-543`, `src/profiles/defaults/supervisor/profile.md:35-47`, `src/vault.py:907-924`, `src/orchestrator/sync_workflow.py:266-278` (only if it references `default_runtime`)
- Test: `tests/test_profile_parser_runtime.py`, `tests/test_runtimes_registry.py` (adjust)

**Interfaces:**
- Produces: `main.py` builds `handler = CommandHandler(orch, config)`, `registry = ToolRegistry()`; `orch.set_command_handler(handler)`; `orch.set_tool_registry(registry)` (new; sets `_tool_registry` and `registry.set_plugin_registry(self.plugin_registry)` when present). `default_registry(config=None) -> RuntimeRegistry` registers nothing. `parser._validate_config` rejects `runtime`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_profile_parser_runtime.py
from src.profiles.parser import _validate_config


def test_runtime_key_rejected_with_pointer_to_harness():
    errors = _validate_config({"runtime": "supervisor", "harness": "claude"})
    assert any("'runtime' was removed" in e and "harness" in e for e in errors)


def test_harness_only_is_fine():
    assert not [e for e in _validate_config({"harness": "claude"}) if "runtime" in e]
```

In `tests/test_runtimes_registry.py`, replace any test asserting the supervisor singleton is registered with:

```python
def test_default_registry_is_empty():
    from src.runtimes import default_registry
    assert default_registry().names() == []
```

- [ ] **Step 2: Run to verify failure** — the parser test fails (no error emitted).

- [ ] **Step 3: Implement**

`src/profiles/parser.py` — after the `agent_name` block (:543):

```python
    # --- runtime --- retired with the in-process Supervisor (llm-direct-path L6).
    if "runtime" in config:
        errors.append(
            "Config 'runtime' was removed; every agent runs as a tmux session. "
            'Select the CLI with \'harness\' ("claude", "codex", "gemini").'
        )
```

`src/profiles/defaults/supervisor/profile.md:38`: delete the `"runtime": "supervisor",` line. `src/vault.py:907-924`: replace the `## Config` block and the paragraph after it with

```markdown
## Config
```json
{
  "harness": "claude",
  "lifecycle": "named",
  "mode": "on_demand",
  "wake_mode": "resume",
  "idle_timeout": 2700,
  "needs_workspace": false
}
```
```

Add a startup strip (spec §6.2) in `src/profiles/sync.py` where vault profiles are read at startup: if the parsed `## Config` JSON contains `runtime`, remove the key, rewrite the file, and log once `"profile %s: removed retired 'runtime' key"` — mirror the existing file-rewrite helper in that module; if none exists, apply the strip in memory only and log a warning telling the user to delete the key.

`src/main.py`: delete lines 91-110 and 122-123; replace with

```python
    from src.commands.handler import CommandHandler
    from src.tools.registry import ToolRegistry

    registry = default_registry(config=config)
    orch._runtimes = registry
    await orch.initialize()
    handler = CommandHandler(orch, config)
    orch.set_command_handler(handler)
    orch.set_tool_registry(ToolRegistry())
```

`src/runtimes/__init__.py::default_registry(supervisor=None, config=None)` → `default_registry(config=None)` returning `RuntimeRegistry(singletons={}, config=config)`; rewrite its docstring to: "No in-tree runtimes are registered: every agent runs as a tmux session selected by the profile's ``harness``. The registry remains the injection seam for tests and for ``sync_workflow``."

`src/orchestrator/core.py`: delete `set_supervisor` (:614-626) and `self._supervisor = None` (:415); add

```python
    def set_tool_registry(self, registry) -> None:
        """Daemon-wide ToolRegistry (tool definitions for playbook nodes / plugins)."""
        self._tool_registry = registry
        plugin_registry = getattr(self, "plugin_registry", None)
        if plugin_registry is not None and hasattr(registry, "set_plugin_registry"):
            registry.set_plugin_registry(plugin_registry)
```

`src/orchestrator/execution.py:352`: `platform_name = profile.runtime if profile else self.config.default_runtime` → `platform_name = ""` (the registry ignores the name; log at debug that a non-session profile is being dispatched through the runtime seam). `agent_reconciler.py:295-307`: replace the try-block with `return True` (no runtime class decides workspace needs any more). `messaging/port.py:31`: delete the TYPE_CHECKING import (and the annotation that used it → `Any`). `config.py`: delete `default_runtime` (:1438-1443) and its validation (:1632-1648); delete `ReflectionConfig`, `ObservationConfig`, and their fields/loader lines in `SupervisorConfig` (keep `global_`); delete `src/reflection.py`. `grep -rn "reflection\.\|observation\." src/ --include=*.py` for stragglers reading `config.supervisor.reflection` (e.g. `config_editor.py` FLAG_NOTES) and remove them.

Delete the files listed above; `git rm` them.

- [ ] **Step 4: Run tests** — `grep -rn "runtimes.supervisor\|Supervisor(" src/ tests/` must be empty. `pytest tests/ -n auto -q` → fix the fallout in the remaining 20 files that imported `src.runtimes.supervisor` only for `Supervisor` (they now use `PlaybookServices.for_tests` or drop the import). Expected: green.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor!: delete the in-process Supervisor; harness is the only agent selector"
```

---

### Task 13: Delete plan discovery; doctor check for stranded `AWAITING_PLAN_APPROVAL` rows

**Files:**
- Modify: `src/orchestrator/approval.py:289-326, 360-404` (delete `_phase_plan_discover`, `_should_run_legacy_plan_region`, `_phase_plan_generate`, `_discover_and_store_plan` and their pipeline registration), `src/orchestrator/execution.py:1034-1055, 1103-1130`, `src/commands/task_commands.py:2888-2915, 3435-3465`, `src/config.py` (`PlannerConfig` :1227-1238 + loader :2429-2433 + `"planner"` in the section sets), `src/prompts/plan_parser_system.md` (delete)
- Create: `src/doctor/plan_checks.py`; register in `src/doctor/__init__.py`
- Test: `tests/test_doctor_plan_checks.py`; delete plan-discovery tests (`grep -rl "break_plan_into_tasks\|legacy_plan_discovery\|_phase_plan_discover" tests/`)

**Interfaces:**
- Produces: doctor check `tasks.awaiting_plan_approval` (report-only, `Severity.WARN` when count > 0).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_doctor_plan_checks.py
from unittest.mock import AsyncMock, MagicMock

from src.doctor.models import DoctorContext, Severity
from src.doctor.plan_checks import run_check


async def test_reports_stranded_rows():
    db = MagicMock()
    db.list_tasks = AsyncMock(return_value=[MagicMock(id="t1"), MagicMock(id="t2")])
    r = await run_check("tasks.awaiting_plan_approval", DoctorContext(config=MagicMock(), db=db))
    assert r.severity == Severity.WARN
    assert r.data["tasks"] == ["t1", "t2"]
    assert "aq task reopen" in r.detail


async def test_ok_when_none():
    db = MagicMock()
    db.list_tasks = AsyncMock(return_value=[])
    r = await run_check("tasks.awaiting_plan_approval", DoctorContext(config=MagicMock(), db=db))
    assert r.severity == Severity.OK
```

- [ ] **Step 2: Run to verify failure** — ImportError.

- [ ] **Step 3: Implement**

```python
# src/doctor/plan_checks.py
"""tasks.awaiting_plan_approval — plan discovery was deleted (llm-direct-path §6.3);
rows left in that state need a human to reopen or close them."""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import TaskStatus

OWNER = "llm-direct-path"
CHECK_ID = "tasks.awaiting_plan_approval"


async def _check(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(id=CHECK_ID, severity=Severity.INFO, detail="database not initialised")
    rows = await ctx.db.list_tasks(status=TaskStatus.AWAITING_PLAN_APPROVAL)
    ids = [t.id for t in rows]
    if not ids:
        return CheckResult(id=CHECK_ID, severity=Severity.OK, detail="no stranded plan-approval tasks")
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(ids)} task(s) in AWAITING_PLAN_APPROVAL; plan discovery was removed — "
            "`aq task reopen <id>` to run them again or `aq task close <id>` to drop them"
        ),
        data={"count": len(ids), "tasks": ids[:50]},
    )


def plan_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=CHECK_ID, run=_check, owner=OWNER)]


CHECKS = plan_checks()


async def run_check(check_id: str, ctx: DoctorContext) -> CheckResult:
    for c in CHECKS:
        if c.id == check_id:
            return await c.run(ctx)
    raise KeyError(check_id)
```

(Confirm `db.list_tasks(status=...)` is the real signature — `grep -n "def list_tasks" src/database/`; adjust to whatever filters by status.) Register `plan_checks()` in `src/doctor/__init__.py::default_registry`.

Deletions: in `approval.py` remove the four methods and the `plan_discover` phase from the completion pipeline's phase list (grep `_phase_plan_discover` for the registration). In `execution.py` remove the `plan_needs_approval` block (:1038-1055) and the `break_plan_into_tasks` block (:1103-1130) — the surrounding pipeline result handling continues as if `plan_needs_approval` were False; delete `pipeline_ctx.plan_needs_approval` and `self._plan_processing_locks` if now unused. In `task_commands.py` remove the two `break_plan_into_tasks` branches: at :2888-2915 the legacy "create subtasks on approval" branch is deleted (the non-legacy branch above it remains the only path); at :3435-3465 delete Phase 1 and leave `created_info = []`. Delete `PlannerConfig`, its field on `AppConfig`, loader, and `"planner"` from `RESTART_REQUIRED_SECTIONS`/`_SECTION_FIELDS`. Delete `src/prompts/plan_parser_system.md`.

- [ ] **Step 4: Run tests** — `grep -rn "break_plan_into_tasks\|legacy_plan_discovery\|plan_parser_system\|_phase_plan" src/ tests/` empty; `pytest tests/ -n auto -q` green after deleting/adjusting the plan-discovery tests.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor!: delete plan.md discovery; doctor check reports stranded AWAITING_PLAN_APPROVAL tasks"
```

---

### Task 14: Delete `src/chat_providers/`, `ChatProviderConfig`, the wizard step, and the rest

**Files:**
- Delete: `src/chat_providers/` (whole directory), `tests/test_logged_provider.py`, `tests/test_compiler_llm_path_removed.py`
- Modify: `src/config.py` (`ChatProviderConfig` :585-624, field :1388, validate :1571, section lists :1736/:1768, loader :2199-2212, `AutoTaskConfig.use_llm_parser`/`llm_parser_model` :201-202 + loader :2286-2287), `src/orchestrator/core.py:416-431` (`_chat_provider` block), `src/plugins/services.py:243, 577-579` (`chat_provider` → `llm`), `src/llm_logger.py` (delete `log_chat_provider_call`, keep `_log_provider_call` + `log_llm_call`), `src/vault_glossary.py:414-496` (delete `extract_concepts_llm` and the LLM branch of `build_from_vault`), `src/setup_wizard.py:1261-1378, 1441-1449, 1516-1530, 1589-1619, 1667-1676, 1700`, `pyproject.toml:55-71`
- Test: `tests/test_llm_logger.py` (the five `log_chat_provider_call` tests → `log_llm_call` / `llm.jsonl`), `tests/test_config_validation.py:134-160` (→ `TestLLMConfigValidation`, moved to `tests/llm/test_config.py` and deleted here), `tests/test_setup_wizard*.py` (drop chat-provider expectations), `tests/test_compiler_llm_path_removed.py` → one assertion in `tests/test_playbook_services.py`:

```python
def test_compiler_never_imports_llm():
    import inspect
    import src.playbooks.compiler as compiler
    src_text = inspect.getsource(compiler)
    assert "src.llm" not in src_text and "create_message" not in src_text
```

- [ ] **Step 1: Update tests as listed** (rename `log_chat_provider_call` → `log_llm_call`, `chat_provider.jsonl` → `llm.jsonl` in `tests/test_llm_logger.py`; delete `TestChatProviderConfigValidation`; add the compiler assertion). Run `pytest tests/test_llm_logger.py -q` → fails (`log_llm_call` exists, but old file name assertions are gone — expect only cleanup-test fallout on `chat_provider.jsonl` names, rename those fixtures' file names to `llm.jsonl` too).

- [ ] **Step 2: Implement deletions**

- `git rm -r src/chat_providers tests/test_logged_provider.py tests/test_compiler_llm_path_removed.py`.
- `config.py`: delete `ChatProviderConfig`, the `chat_provider` field, its `validate()` line, both section-list entries, and the `if "chat_provider" in raw:` loader block (the Task 1 `llm` loader already handles the legacy key). Delete `use_llm_parser` and `llm_parser_model` from `AutoTaskConfig` and its loader.
- `core.py:416-431`: delete the `_chat_provider` block.
- `plugins/services.py`: protocol property `chat_provider` → `llm` returning `self._config.llm` (grep `services.chat_provider` / `.chat_provider` in `src/plugins/` for callers — none expected).
- `llm_logger.py`: delete `log_chat_provider_call`; rename `_log_provider_call` → keep private; update the class docstring (`llm.jsonl`).
- `vault_glossary.py`: delete `extract_concepts_llm` and any `chat_provider` parameter on `build_from_vault`.
- `setup_wizard.py`: delete `step_chat_provider` and the Ollama helpers it alone used (`_is_ollama_installed`, `_install_ollama`, `_is_ollama_running`, `_start_ollama`, `_ollama_list_models`, `_pull_ollama_model` — confirm no other caller with grep); remove the `chat_provider_cfg` parameter from `step_write_config` and `step_test_connectivity` and the call at :1671/:1675/:1700; renumber step headers; in `step_write_config` replace :1516-1530 with

```python
    yaml_lines.append("llm:")
    yaml_lines.append("  provider: anthropic      # anthropic | google | openai")
    yaml_lines.append("  default_class: fast-medium")
    yaml_lines.append("  # api_key: ...           # or ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY")
    yaml_lines.append("")
```

- `pyproject.toml`: `gemini = [...]` → `google = ["google-genai>=1.0.0"]`, `ollama = [...]` → `openai = ["openai>=1.0.0"]`, add `llm = ["anthropic>=0.42.0", "google-genai>=1.0.0", "openai>=1.0.0"]`.
- `src/config_editor.py` and `src/cli/system_config.py`, `src/tools/definitions.py`, `src/api/models/system.py`: grep `chat_provider` and rename to `llm` where it is a section name.

- [ ] **Step 3: Verify** — `grep -rn "chat_provider\|chat_providers\|ChatProviderConfig\|log_chat_provider_call\|thinking_budget" src/ tests/ pyproject.toml` returns nothing except the legacy-key handling in `config.py` (`raw.get("chat_provider")`, `normalize_llm_provider`, and the deprecation warning text). `ruff check src tests` clean. Full suite green: `timeout 580 /home/jkern/dev/agent-queue2/.venv/bin/python -m pytest tests/ -n auto -q -p no:cacheprovider`. Then `scripts/e2e-env.sh --reset && scripts/e2e-smoke.sh` green.

- [ ] **Step 4: Commit (Phase 3 boundary)**

```bash
git add -A
git commit -m "refactor!: remove src/chat_providers, ChatProviderConfig, the wizard provider step, and dead LLM code"
```

---

# Phase 4 — docs (spec §6.4)

### Task 15: Documentation and spec addendum

**Files:**
- Delete: `docs/specs/chat-providers/` (5 files)
- Modify: `docs/specs/config.md:171-184` (§4.6 → `llm`), `:244`, `:257`; `docs/specs/llm-logging.md` (`chat_provider.jsonl` → `llm.jsonl`, `LoggedChatProvider` → `LLMClient` logging, `caller` field); `docs/specs/orchestrator.md:169, 176, 883` and `docs/specs/plan-parser.md` (remove the LLM plan-parser rows / mark the page superseded by the `planner` profile); `docs/specs/plugin-system.md:530`; `docs/specs/setup-wizard.md:191, 241, 252, 268`; `docs/specs/design/supervisor-agent.md` §10 table (every row → **Removed** with a pointer to `docs/superpowers/specs/2026-08-30-llm-direct-path-design.md`; S9 text updated); `docs/specs/design/feature-pauses.md:216`; `docs/guides/architecture.md:212`; `docs/agent-queue-primitives.md:209, 235`; `CLAUDE.md` (Runtimes bullet → "`src/runtimes/` keeps only the `Runtime` ABC + registry as the non-session dispatch seam; nothing is registered in production" and add a **LLM direct path** bullet: `src/llm/` — `LLMClient.complete/run_tools`, `LLMCallSpec`, config `llm:`, intelligence classes shared with sessions; consumers: playbook nodes/transitions, plugin `invoke_llm`, stub enricher, vault summaries); `profile.md:204, 261`; the spec file itself (add a "Deviations applied during implementation" section listing the three items from this plan's header).

- [ ] **Step 1: Make the edits.** For `config.md` §4.6 use:

```markdown
### 4.6 `llm` — the direct LLM path

| Key | Default | Meaning |
|---|---|---|
| `provider` | `anthropic` | `anthropic` \| `google` \| `openai` (Ollama = `openai` + `base_url`) |
| `model` | `""` | explicit model id; empty = intelligence class, else the adapter's default |
| `api_key` | `""` | optional; `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY` otherwise |
| `base_url` | `""` | `openai` only: an OpenAI-compatible endpoint, e.g. `http://localhost:11434/v1` |
| `max_tokens` | `4096` | response budget when a call names none |
| `default_class` | `""` | intelligence class for calls that name none (`vault/intelligence-classes/*.md`) |

The legacy `chat_provider:` block still loads (ids `gemini→google`, `ollama→openai`) with a one-time deprecation warning. Used by playbook nodes and transitions, plugin `invoke_llm`, reference-stub enrichment, and `aq vault rebuild-index --with-summaries`. Coding agents never use it — they are tmux sessions selected by the profile's `harness`.
```

- [ ] **Step 2: Verify** — `grep -rn "chat_provider\|chat_providers\|Supervisor.chat\|runtime: supervisor" docs/specs docs/guides CLAUDE.md profile.md` shows only historical notes in `docs/superpowers/plans/2026-08-21-*` and analysis files (leave those) plus the deprecation sentence in `config.md`.

- [ ] **Step 3: Commit (Phase 4 boundary)**

```bash
git add -A
git commit -m "docs: llm direct path — config.md, llm-logging, supervisor-agent §10, CLAUDE.md, spec addendum"
```

Then `superpowers:finishing-a-development-branch` — merge `llm-direct-path` into `main` after a final full-suite run.
