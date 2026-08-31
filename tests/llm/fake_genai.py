"""A deterministic stand-in for the ``google.genai`` SDK.

The Gemini adapter and ``GoogleProvider`` import ``google.genai`` lazily,
so the tests in this package install these shims into ``sys.modules`` and
assert on what the adapter *built* rather than on what a real SDK would
send.  Keeping the fake here means the provider-translation tests run
identically whether or not ``google-genai`` is installed.
"""

from __future__ import annotations

import sys
import types as _pytypes
from dataclasses import dataclass, field
from typing import Any


class FakeSchema:
    """Records the kwargs ``_convert_schema`` passed to ``types.Schema``."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    @property
    def type(self) -> Any:
        return self.kwargs.get("type")

    @property
    def properties(self) -> dict[str, "FakeSchema"]:
        return self.kwargs.get("properties", {})

    @property
    def items(self) -> "FakeSchema | None":
        return self.kwargs.get("items")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FakeSchema({self.kwargs!r})"


@dataclass
class FakeFunctionDeclaration:
    name: str
    description: str = ""
    parameters: Any = None


@dataclass
class FakeTool:
    function_declarations: list[FakeFunctionDeclaration] = field(default_factory=list)


@dataclass
class FakePart:
    """A part carrying exactly one of text / function call / function response."""

    text: str | None = None
    function_call: Any = None
    function_response: Any = None

    @staticmethod
    def from_text(*, text: str) -> "FakePart":
        return FakePart(text=text)

    @staticmethod
    def from_function_call(*, name: str, args: dict) -> "FakePart":
        return FakePart(function_call=_pytypes.SimpleNamespace(name=name, args=args))

    @staticmethod
    def from_function_response(*, name: str, response: dict) -> "FakePart":
        return FakePart(function_response=_pytypes.SimpleNamespace(name=name, response=response))


@dataclass
class FakeContent:
    role: str
    parts: list[FakePart] = field(default_factory=list)


@dataclass
class FakeThinkingConfig:
    thinking_budget: int = 0


@dataclass
class FakeGenerateContentConfig:
    system_instruction: str = ""
    max_output_tokens: int = 0
    thinking_config: FakeThinkingConfig | None = None
    tools: list = field(default_factory=list)


def build_types_module() -> _pytypes.ModuleType:
    """Return a module object shaped like ``google.genai.types``."""
    mod = _pytypes.ModuleType("google.genai.types")
    mod.Schema = FakeSchema
    mod.FunctionDeclaration = FakeFunctionDeclaration
    mod.Tool = FakeTool
    mod.Part = FakePart
    mod.Content = FakeContent
    mod.ThinkingConfig = FakeThinkingConfig
    mod.GenerateContentConfig = FakeGenerateContentConfig
    return mod


def install(monkeypatch, *, client_factory=None) -> _pytypes.ModuleType:
    """Install the fake ``google`` / ``google.genai`` modules for one test.

    ``client_factory`` becomes ``google.genai.Client``.  Returns the fake
    ``google.genai.types`` module so callers can reference its classes.
    """
    types_mod = build_types_module()

    genai_mod = _pytypes.ModuleType("google.genai")
    genai_mod.types = types_mod
    if client_factory is not None:
        genai_mod.Client = client_factory

    google_mod = _pytypes.ModuleType("google")
    google_mod.genai = genai_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    return types_mod
