"""Tests for ``Supervisor._merge_profile_into_chat_config``.

The supervisor profile at ``vault/agent-types/supervisor/profile.md``
can override the chat-provider settings in ``config.chat_provider`` via
its ``## Config`` JSON block.  Provider-semantic fields (``provider``,
``model``, token budgets, ``thinking_budget``, etc.) win over the yaml
config; environment-specific fields (``api_key``, ``base_url``) always
come from the yaml config.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


from src.config import ChatProviderConfig
from src.runtimes.supervisor import Supervisor


@dataclass
class _FakeAppConfig:
    """Minimal AppConfig surrogate — avoids pulling in the full loader."""
    data_dir: str
    chat_provider: ChatProviderConfig


def _write_supervisor_profile(data_dir: Path, config_block: str) -> None:
    profile_path = data_dir / "vault" / "agent-types" / "supervisor" / "profile.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        f"""---
id: supervisor
name: Supervisor
---

# Supervisor

## Role
Coordinate.

## Config
```json
{config_block}
```
""",
        encoding="utf-8",
    )


def _make_supervisor(data_dir: Path, base_cfg: ChatProviderConfig) -> Supervisor:
    cfg = _FakeAppConfig(data_dir=str(data_dir), chat_provider=base_cfg)
    # Supervisor's __init__ needs orch but the code paths under test never
    # touch it, so a MagicMock is fine.
    sup = Supervisor.__new__(Supervisor)
    sup.config = cfg  # type: ignore[attr-defined]
    return sup


def test_no_profile_returns_base_config(tmp_path: Path) -> None:
    base = ChatProviderConfig(
        provider="anthropic", model="claude-sonnet-4-6", api_key="base-key"
    )
    sup = _make_supervisor(tmp_path, base)
    out = sup._merge_profile_into_chat_config(base)
    assert out is base


def test_profile_overrides_provider_but_not_model(tmp_path: Path) -> None:
    """``provider`` wins from the profile; ``model`` deliberately does not.

    Since the supervisor became a named tmux session, ``model`` in its profile
    means *the model the CLI is launched with* (``claude --model ...``) and is
    necessarily a Claude model. Feeding that to the chat provider — Gemini in
    this deployment — built a Gemini client asking for ``claude-opus-5`` and
    broke every in-process LLM call. One key cannot serve both paths, so the
    session keeps ``model`` and the chat provider reads config.chat_provider.
    """
    base = ChatProviderConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="base-key",
        base_url="",
        max_tokens=1024,
    )
    _write_supervisor_profile(
        tmp_path,
        '{"provider": "gemini", "model": "gemini-2.5-flash"}',
    )

    sup = _make_supervisor(tmp_path, base)
    out = sup._merge_profile_into_chat_config(base)

    assert out.provider == "gemini"
    # ``model`` is NOT taken from the profile — it belongs to the session.
    assert out.model == "claude-sonnet-4-6"
    # Environment-specific fields are preserved from yaml config.
    assert out.api_key == "base-key"
    # Unchanged fields preserved.
    assert out.max_tokens == 1024


def test_profile_overrides_token_budgets(tmp_path: Path) -> None:
    base = ChatProviderConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        max_tokens=1024,
        playbook_max_tokens=2048,
        thinking_budget=0,
    )
    _write_supervisor_profile(
        tmp_path,
        '{"max_tokens": 4096, "playbook_max_tokens": 8192, "thinking_budget": 16384}',
    )

    sup = _make_supervisor(tmp_path, base)
    out = sup._merge_profile_into_chat_config(base)

    assert out.max_tokens == 4096
    assert out.playbook_max_tokens == 8192
    assert out.thinking_budget == 16384
    # Other fields untouched.
    assert out.provider == "gemini"
    assert out.model == "gemini-2.5-flash"


def test_profile_empty_config_block_is_ignored(tmp_path: Path) -> None:
    """Empty ``## Config`` block falls back to the yaml config entirely."""
    base = ChatProviderConfig(provider="anthropic", model="claude-sonnet-4-6")
    _write_supervisor_profile(tmp_path, "{}")
    sup = _make_supervisor(tmp_path, base)
    out = sup._merge_profile_into_chat_config(base)
    # Empty overrides → identical config returned (could be `base` itself).
    assert out.provider == "anthropic"
    assert out.model == "claude-sonnet-4-6"


def test_profile_partial_override_preserves_other_fields(tmp_path: Path) -> None:
    """A partial override touches only the keys it names."""
    base = ChatProviderConfig(
        provider="gemini",
        model="gemini-2.5-flash",
        api_key="k",
        max_tokens=999,
    )
    _write_supervisor_profile(tmp_path, '{"max_tokens": 4096}')

    sup = _make_supervisor(tmp_path, base)
    out = sup._merge_profile_into_chat_config(base)

    assert out.max_tokens == 4096
    assert out.provider == "gemini"  # unchanged
    assert out.api_key == "k"
    assert out.model == "gemini-2.5-flash"  # unchanged


def test_profile_model_never_reaches_the_chat_provider(tmp_path: Path) -> None:
    """Regression: a Claude session model must not configure a Gemini client.

    The live failure was ``model: claude-opus-5`` in the supervisor profile —
    correct for ``claude --model`` — silently becoming the Gemini client's
    model and breaking playbook nodes, reflection and plan discovery.
    """
    base = ChatProviderConfig(provider="gemini", model="gemini-2.5-flash", api_key="k")
    _write_supervisor_profile(tmp_path, '{"model": "claude-opus-5"}')

    sup = _make_supervisor(tmp_path, base)
    out = sup._merge_profile_into_chat_config(base)

    assert out.model == "gemini-2.5-flash"
    assert out.provider == "gemini"


def test_invalid_json_in_profile_falls_back(tmp_path: Path) -> None:
    """Malformed JSON doesn't crash; base is returned."""
    base = ChatProviderConfig(provider="anthropic", model="claude-sonnet-4-6")
    profile_path = tmp_path / "vault" / "agent-types" / "supervisor" / "profile.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        """---
id: supervisor
name: Supervisor
---

## Config
```json
{not valid json
```
""",
        encoding="utf-8",
    )

    sup = _make_supervisor(tmp_path, base)
    out = sup._merge_profile_into_chat_config(base)
    # Whatever the parser does with malformed JSON, the supervisor should
    # return a usable config — never crash.
    assert isinstance(out, ChatProviderConfig)
