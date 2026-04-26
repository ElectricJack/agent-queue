# Platforms Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename `src/adapters/` to `src/platforms/`, declare a typed `Capability` enum, ship three platform implementations (`ClaudeSDKPlatform` renamed from existing `ClaudeAdapter`, plus new `ClaudeCLIPlatform` and `CodexCLIPlatform`), wire a `PlatformRegistry`, and switch orchestrator call sites from hardcoded `"claude"` to a config-driven `default_platform` selector.

**Architecture:** A four-method ABC (`Platform`) backed by a `frozenset[Capability]` class-level declaration of what each platform can do. Two CLI-based platforms share an `_subprocess.py` helper module that owns subprocess spawning, NDJSON line streaming, signal-based cancellation, and env-var isolation. Profile-to-config translation moves out of the central factory and into each platform's `__init__`, so the registry's only job is class lookup by string name.

**Tech Stack:** Python 3.12+, `asyncio.subprocess`, ruff (line-length 100), pytest with pytest-asyncio (auto mode), pytest-xdist for parallel runs. The `claude` CLI (v2.1.116) and `codex` CLI (v0.125.0) are installed at `~/.local/bin/`.

---

## Spec Reference

This plan implements `docs/superpowers/specs/2026-04-25-platforms-implementation-design.md` (commit `5aa2b4b9` and earlier). Section numbers below (e.g., "spec §2") refer to that document.

Phase 2 (profile schema, validation, Discord surfacing, task-creation gate, migration) is a separate spec at `docs/superpowers/specs/2026-04-25-profile-validation-design.md` and gets its own implementation plan after this one lands.

## Working Directory

Run all commands from `/home/jkern/dev/agent-queue2` unless otherwise noted.

## Worktree (optional)

Consider running this in a worktree to keep the rename diff isolated from other work:

```bash
git worktree add ../agent-queue2-platforms -b platforms-impl
cd ../agent-queue2-platforms
```

Not required — the plan also works in the main checkout.

## Pre-flight check

- [ ] **Verify CLIs are installed**

Run:
```bash
claude --version && codex --version
```
Expected:
```
2.1.116 (Claude Code)
codex-cli 0.125.0
```

If either is missing, install: `npm install -g @anthropic-ai/claude-code @openai/codex` and symlink the codex binary to `~/.local/bin/codex` if `which codex` doesn't find it.

- [ ] **Confirm current tests are green**

Run:
```bash
pytest tests/test_adapters.py -v 2>&1 | tail -20
```
Expected: all pass. Establishes the rename baseline.

---

## File Structure Overview

| Path | Purpose | Status |
|---|---|---|
| `src/platforms/__init__.py` | `PlatformRegistry`, package exports | new |
| `src/platforms/base.py` | `Platform` ABC, `Capability` enum, `MessageCallback` re-export | new |
| `src/platforms/claude_sdk.py` | `ClaudeSDKPlatform` (renamed `ClaudeAdapter`) | moved |
| `src/platforms/_subprocess.py` | `run_streaming_subprocess`, `parse_ndjson_line`, `isolated_env` | new |
| `src/platforms/claude_cli.py` | `ClaudeCLIPlatform` wrapping `claude -p --output-format stream-json` | new |
| `src/platforms/codex_cli.py` | `CodexCLIPlatform` wrapping `codex exec --json` | new |
| `tests/test_platforms_base.py` | ABC contract + Capability enum + MockPlatform | new (replaces ABC parts of test_adapters.py) |
| `tests/test_platforms_subprocess.py` | helpers tests | new |
| `tests/test_platforms_registry.py` | registry tests | new |
| `tests/test_platforms_claude_sdk.py` | SDK platform tests (moved from test_adapters.py + test_claude_usage.py) | renamed |
| `tests/test_platforms_claude_cli.py` | CLI platform tests | new |
| `tests/test_platforms_codex_cli.py` | Codex platform tests | new |
| `src/adapters/` | retired | deleted |
| `tests/test_adapters.py` | retired (content split into platform tests + non-platform L0/L1 tests stay where consumers reference) | mostly deleted, residual content moved |
| `src/orchestrator/execution.py` | call site change at line ~411 | modified |
| `src/orchestrator/sync_workflow.py` | call site change at line ~273 | modified |
| `src/orchestrator/core.py` | rename `_adapter_factory` → `_platforms`, update docstrings | modified |
| `src/main.py` | wire `PlatformRegistry` instead of `AdapterFactory` | modified |
| `src/config.py` | add `AppConfig.default_platform: str = "claude_sdk"` | modified |
| `docs/specs/adapters/` | renamed to `docs/specs/platforms/`, content updated | renamed |

---

## Task 1: Platform ABC, Capability enum, MessageCallback

**Files:**
- Create: `src/platforms/__init__.py` (initially just package marker — registry added in Task 3)
- Create: `src/platforms/base.py`
- Create: `tests/test_platforms_base.py`

- [ ] **Step 1: Create empty package marker**

Create `src/platforms/__init__.py` with content:
```python
"""Platforms layer: pluggable AI agent backends.

The orchestrator interacts with platforms exclusively through the
:class:`Platform` ABC defined in :mod:`src.platforms.base`.  This module
exposes :class:`PlatformRegistry` (added in Task 3) for looking up
platform classes by string name.
"""

from __future__ import annotations
```

- [ ] **Step 2: Write the failing test for the ABC and enum**

Create `tests/test_platforms_base.py`:
```python
"""Tests for the Platform ABC, Capability enum, and ABC-conforming MockPlatform."""

from __future__ import annotations

import pytest

from src.platforms.base import Capability, MessageCallback, Platform
from src.models import AgentOutput, AgentResult, TaskContext


class TestCapabilityEnum:
    def test_is_str_enum(self):
        assert isinstance(Capability.MCP, str)
        assert Capability.MCP == "mcp"

    def test_required_members_exist(self):
        for name in (
            "MCP",
            "PLAN_MODE",
            "RESUME",
            "STREAMING_JSON",
            "HOOKS",
            "SKILLS",
            "MEMORY_MD",
            "THINKING",
            "PERMISSION_CALLBACKS",
        ):
            assert hasattr(Capability, name), f"Capability.{name} missing"

    def test_values_are_lowercase_snake(self):
        for member in Capability:
            assert member.value == member.value.lower()
            assert " " not in member.value


class TestPlatformABC:
    def test_cannot_instantiate_bare_abc(self):
        with pytest.raises(TypeError):
            Platform()  # type: ignore[abstract]

    def test_subclass_missing_methods_cannot_instantiate(self):
        class Incomplete(Platform):
            name = "incomplete"
            capabilities = frozenset()

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_with_all_methods_instantiates(self):
        class Complete(Platform):
            name = "complete"
            capabilities = frozenset({Capability.STREAMING_JSON})

            async def start(self, task: TaskContext) -> None:  # noqa: ARG002
                return

            async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:  # noqa: ARG002
                return AgentOutput(result=AgentResult.COMPLETED)

            async def stop(self) -> None:
                return

            async def is_alive(self) -> bool:
                return False

        inst = Complete()
        assert inst.name == "complete"
        assert Capability.STREAMING_JSON in inst.capabilities


class MockPlatform(Platform):
    """Minimal Platform impl for use across the test suite (replaces MockAdapter)."""

    name = "mock"
    capabilities = frozenset()

    def __init__(self, result: AgentResult = AgentResult.COMPLETED, tokens: int = 1000):
        self._result = result
        self._tokens = tokens
        self.started = False
        self.stopped = False

    async def start(self, task: TaskContext) -> None:  # noqa: ARG002
        self.started = True

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:  # noqa: ARG002
        return AgentOutput(
            result=self._result,
            summary="Did the thing",
            tokens_used=self._tokens,
        )

    async def stop(self) -> None:
        self.stopped = True

    async def is_alive(self) -> bool:
        return self.started and not self.stopped


class TestMockPlatform:
    async def test_lifecycle(self):
        platform = MockPlatform()
        ctx = TaskContext(description="test task")
        await platform.start(ctx)
        assert platform.started
        assert await platform.is_alive()
        output = await platform.wait()
        assert output.result == AgentResult.COMPLETED
        assert output.tokens_used == 1000
        await platform.stop()
        assert platform.stopped

    async def test_failed_result(self):
        platform = MockPlatform(result=AgentResult.FAILED)
        ctx = TaskContext(description="test")
        await platform.start(ctx)
        output = await platform.wait()
        assert output.result == AgentResult.FAILED

    async def test_paused_result(self):
        platform = MockPlatform(result=AgentResult.PAUSED_RATE_LIMIT)
        ctx = TaskContext(description="test")
        await platform.start(ctx)
        output = await platform.wait()
        assert output.result == AgentResult.PAUSED_RATE_LIMIT
```

- [ ] **Step 3: Run the test to verify it fails**

Run:
```bash
pytest tests/test_platforms_base.py -v
```
Expected: ImportError or ModuleNotFoundError for `src.platforms.base`.

- [ ] **Step 4: Implement `src/platforms/base.py`**

Create `src/platforms/base.py`:
```python
"""Platform ABC -- the contract between the orchestrator and any AI agent.

The interface is intentionally minimal: ``start``, ``wait``, ``stop``,
``is_alive``.  Two ClassVars (``name``, ``capabilities``) declare the
platform's identity in the registry and the typed feature set it
supports; phase 2 of the platforms refactor consumes these for profile
validation.

Lifecycle:
  1. ``start(task)`` receives a :class:`TaskContext` with workspace,
     description, criteria, and attached context.
  2. ``wait(on_message)`` blocks until the agent finishes; while running
     it streams progress via :data:`MessageCallback`.
  3. ``stop()`` cooperatively cancels the agent.
  4. ``is_alive()`` lets the heartbeat monitor detect dead agents.

Profile binding stays at construction (each platform's ``__init__``
takes ``profile: AgentProfile``), preserving the shape of the legacy
``AgentAdapter``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Awaitable, Callable, ClassVar

from src.models import AgentOutput, TaskContext


# Callback invoked with each human-readable message chunk as the agent works.
# The orchestrator typically wires this to a Discord thread for live output.
MessageCallback = Callable[[str], Awaitable[None]]


class Capability(StrEnum):
    """Typed declaration of features a platform supports.

    Platforms list their capabilities at the class level
    (``capabilities: ClassVar[frozenset[Capability]]``).  Phase 2 of the
    platforms refactor consumes these for sync-time profile validation
    and runtime field filtering.  In phase 1 the enum is declared and
    populated but not yet read by any consumer.
    """

    MCP = "mcp"
    PLAN_MODE = "plan_mode"
    RESUME = "resume"
    STREAMING_JSON = "streaming_json"
    HOOKS = "hooks"
    SKILLS = "skills"
    MEMORY_MD = "memory_md"
    THINKING = "thinking"
    PERMISSION_CALLBACKS = "permission_callbacks"


class Platform(ABC):
    """Base class for AI agent platforms (e.g. ClaudeSDK, ClaudeCLI, CodexCLI)."""

    name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]

    @abstractmethod
    async def start(self, task: TaskContext) -> None:
        """Launch the agent process with the given task."""

    @abstractmethod
    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        """Wait for the agent to finish and return results."""

    @abstractmethod
    async def stop(self) -> None:
        """Cooperatively stop the agent."""

    @abstractmethod
    async def is_alive(self) -> bool:
        """Check if the agent process is still running."""
```

- [ ] **Step 5: Run the test to verify it passes**

Run:
```bash
pytest tests/test_platforms_base.py -v
```
Expected: all tests pass.

- [ ] **Step 6: Run linter**

Run:
```bash
ruff check src/platforms/ tests/test_platforms_base.py
```
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/platforms/__init__.py src/platforms/base.py tests/test_platforms_base.py
git commit -m "Add Platform ABC and Capability enum

Replaces AgentAdapter in src/platforms/base.py. Capability is a closed
StrEnum used in phase 2 for profile validation; declared and populated
here so platform impls get the contract right from the start."
```

---

## Task 2: Subprocess and NDJSON helpers

**Files:**
- Create: `src/platforms/_subprocess.py`
- Create: `tests/test_platforms_subprocess.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_platforms_subprocess.py`:
```python
"""Tests for shared subprocess + NDJSON helpers used by CLI platforms."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from src.platforms._subprocess import (
    isolated_env,
    parse_ndjson_line,
    run_streaming_subprocess,
)


class TestParseNdjsonLine:
    def test_valid_object(self):
        assert parse_ndjson_line(b'{"a": 1}\n') == {"a": 1}

    def test_valid_object_no_trailing_newline(self):
        assert parse_ndjson_line(b'{"a": 1}') == {"a": 1}

    def test_empty_line_returns_none(self):
        assert parse_ndjson_line(b"") is None

    def test_whitespace_only_returns_none(self):
        assert parse_ndjson_line(b"   \n") is None

    def test_malformed_json_returns_none(self, caplog):
        result = parse_ndjson_line(b"not json\n")
        assert result is None
        assert any("malformed" in r.message.lower() for r in caplog.records)

    def test_non_object_returns_none(self):
        # JSON arrays/scalars at the top level aren't useful for our streams.
        assert parse_ndjson_line(b"[1,2,3]\n") is None
        assert parse_ndjson_line(b'"string"\n') is None
        assert parse_ndjson_line(b"42\n") is None


class TestIsolatedEnv:
    def test_strips_claude_session_vars(self):
        with patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "x", "PATH": "/usr/bin"}):
            env = isolated_env()
            assert "CLAUDECODE" not in env
            assert "CLAUDE_CODE_ENTRYPOINT" not in env
            assert env["PATH"] == "/usr/bin"

    def test_extra_overrides_inherited(self):
        with patch.dict(os.environ, {"FOO": "from-os"}):
            env = isolated_env(extra={"FOO": "from-arg"})
            assert env["FOO"] == "from-arg"

    def test_extra_added_when_not_inherited(self):
        env = isolated_env(extra={"NEW_VAR": "value"})
        assert env["NEW_VAR"] == "value"


class TestRunStreamingSubprocess:
    @pytest.mark.asyncio
    async def test_streams_stdout_lines(self, tmp_path):
        # A trivial subprocess that emits two NDJSON lines and exits.
        script = tmp_path / "emit.py"
        script.write_text(
            'import sys; sys.stdout.write(\'{"a":1}\\n{"b":2}\\n\'); sys.stdout.flush()\n'
        )
        lines: list[bytes] = []
        cancel = asyncio.Event()

        exit_code = await run_streaming_subprocess(
            cmd=["python3", str(script)],
            env=isolated_env(),
            cwd=str(tmp_path),
            on_line=lambda b: lines.append(b),
            cancel_event=cancel,
        )

        assert exit_code == 0
        assert lines == [b'{"a":1}\n', b'{"b":2}\n']

    @pytest.mark.asyncio
    async def test_cancellation_terminates(self, tmp_path):
        # A subprocess that would loop forever; we cancel after a short delay.
        script = tmp_path / "loop.py"
        script.write_text("import time\nwhile True:\n    time.sleep(0.05)\n")
        cancel = asyncio.Event()

        async def cancel_soon():
            await asyncio.sleep(0.2)
            cancel.set()

        asyncio.create_task(cancel_soon())
        exit_code = await run_streaming_subprocess(
            cmd=["python3", str(script)],
            env=isolated_env(),
            cwd=str(tmp_path),
            on_line=lambda _: None,
            cancel_event=cancel,
        )
        # SIGTERM exit codes are negative on POSIX (signal number negated)
        # or may be the explicit code SIGTERM=15 on some systems.
        assert exit_code != 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_returned(self, tmp_path):
        script = tmp_path / "fail.py"
        script.write_text("import sys; sys.exit(7)\n")
        cancel = asyncio.Event()

        exit_code = await run_streaming_subprocess(
            cmd=["python3", str(script)],
            env=isolated_env(),
            cwd=str(tmp_path),
            on_line=lambda _: None,
            cancel_event=cancel,
        )
        assert exit_code == 7
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_platforms_subprocess.py -v
```
Expected: ImportError on `src.platforms._subprocess`.

- [ ] **Step 3: Implement `src/platforms/_subprocess.py`**

Create `src/platforms/_subprocess.py`:
```python
"""Shared subprocess + NDJSON helpers for CLI-based platforms.

Both :class:`ClaudeCLIPlatform` and :class:`CodexCLIPlatform` use these
to manage subprocess lifecycle, stream stdout line-by-line, parse NDJSON
events, and isolate environment variables.  Keep platform-specific
parsing / classification out of this module — only generic plumbing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from typing import Callable

logger = logging.getLogger(__name__)

# Env vars that must be stripped before launching agent subprocesses to
# prevent the SDK / CLI from detecting it's inside an existing Claude session.
_STRIP_ENV_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def isolated_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build an env dict for agent subprocesses.

    Inherits :data:`os.environ` minus :data:`_STRIP_ENV_VARS`, then merges
    *extra* on top.  Use this for every subprocess launched by a platform.
    """
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_VARS}
    if extra:
        env.update(extra)
    return env


def parse_ndjson_line(line: bytes) -> dict | None:
    """Parse one NDJSON line into a dict, returning ``None`` for empty / malformed input.

    Streams from CLI platforms occasionally emit blank lines or malformed
    chunks; we log and skip rather than aborting the whole stream.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as e:
        logger.debug("malformed NDJSON line skipped: %s (line=%r)", e, line[:200])
        return None
    if not isinstance(obj, dict):
        logger.debug("NDJSON line was not an object: %r", obj)
        return None
    return obj


async def run_streaming_subprocess(
    cmd: list[str],
    env: dict[str, str],
    cwd: str,
    on_line: Callable[[bytes], None],
    cancel_event: asyncio.Event,
    *,
    sigterm_grace_seconds: float = 2.0,
) -> int:
    """Run *cmd* and stream stdout lines through *on_line*.

    Returns the subprocess exit code.  When *cancel_event* is set, sends
    SIGTERM, waits up to *sigterm_grace_seconds* for graceful exit, then
    SIGKILL.  *on_line* is called synchronously for each raw line
    (including trailing newline) as it arrives.

    Stderr is captured and logged; not surfaced to *on_line*.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def _read_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            on_line(line)

    async def _read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            logger.debug("subprocess stderr: %s", line.rstrip().decode(errors="replace"))

    async def _watch_cancel() -> None:
        await cancel_event.wait()
        if proc.returncode is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=sigterm_grace_seconds)
            except asyncio.TimeoutError:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

    stdout_task = asyncio.create_task(_read_stdout())
    stderr_task = asyncio.create_task(_read_stderr())
    cancel_task = asyncio.create_task(_watch_cancel())

    try:
        await proc.wait()
    finally:
        cancel_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        try:
            await cancel_task
        except (asyncio.CancelledError, Exception):
            pass

    return proc.returncode if proc.returncode is not None else -1
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_platforms_subprocess.py -v
```
Expected: all pass.

- [ ] **Step 5: Run ruff**

Run:
```bash
ruff check src/platforms/_subprocess.py tests/test_platforms_subprocess.py
```
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/platforms/_subprocess.py tests/test_platforms_subprocess.py
git commit -m "Add shared subprocess + NDJSON helpers for CLI platforms

Provides run_streaming_subprocess (with SIGTERM/SIGKILL cancellation),
parse_ndjson_line (skips empty / malformed), and isolated_env (strips
CLAUDECODE markers). Used by ClaudeCLIPlatform and CodexCLIPlatform."
```

---

## Task 3: PlatformRegistry skeleton

**Files:**
- Modify: `src/platforms/__init__.py`
- Create: `tests/test_platforms_registry.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_platforms_registry.py`:
```python
"""Tests for PlatformRegistry."""

from __future__ import annotations

import pytest

from src.platforms import PlatformRegistry
from src.platforms.base import Capability, MessageCallback, Platform
from src.models import AgentOutput, AgentResult, TaskContext


class _FakePlatform(Platform):
    name = "fake"
    capabilities = frozenset({Capability.STREAMING_JSON})

    def __init__(self, profile=None, llm_logger=None):
        self.profile = profile
        self.llm_logger = llm_logger

    async def start(self, task: TaskContext) -> None:
        return

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        return AgentOutput(result=AgentResult.COMPLETED)

    async def stop(self) -> None:
        return

    async def is_alive(self) -> bool:
        return False


class TestPlatformRegistry:
    def test_empty_registry_unknown_returns_none(self):
        reg = PlatformRegistry(platforms={})
        assert reg.get("anything") is None

    def test_register_and_get(self):
        reg = PlatformRegistry(platforms={"fake": _FakePlatform})
        assert reg.get("fake") is _FakePlatform

    def test_names_returns_registered_keys(self):
        reg = PlatformRegistry(platforms={"fake": _FakePlatform, "other": _FakePlatform})
        assert sorted(reg.names()) == ["fake", "other"]

    def test_create_returns_instance_with_profile_and_logger(self):
        reg = PlatformRegistry(platforms={"fake": _FakePlatform})
        inst = reg.create("fake", profile="P", llm_logger="L")
        assert isinstance(inst, _FakePlatform)
        assert inst.profile == "P"
        assert inst.llm_logger == "L"

    def test_create_unknown_raises_value_error(self):
        reg = PlatformRegistry(platforms={"fake": _FakePlatform})
        with pytest.raises(ValueError, match="Unknown platform"):
            reg.create("nope", profile=None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_platforms_registry.py -v
```
Expected: ImportError for `PlatformRegistry`.

- [ ] **Step 3: Implement `PlatformRegistry`**

Edit `src/platforms/__init__.py`. Replace its content with:
```python
"""Platforms layer: pluggable AI agent backends.

The orchestrator interacts with platforms exclusively through the
:class:`Platform` ABC defined in :mod:`src.platforms.base`.  This module
exposes :class:`PlatformRegistry` for looking up platform classes by
string name; the registry is the single source of truth for which
platforms a running daemon supports.

Plugin-as-platform support (registering platforms from external plugins
via ``PluginContext.register_platform``) is a future swap of the
internal dict for a plugin-discovery hook — the registry's external
shape stays the same.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.platforms.base import Capability, MessageCallback, Platform

if TYPE_CHECKING:
    from src.models import AgentProfile

__all__ = ["Capability", "MessageCallback", "Platform", "PlatformRegistry"]


class PlatformRegistry:
    """Looks up :class:`Platform` classes by name.

    Construction takes an explicit ``platforms`` dict so tests can build
    isolated registries.  Production wiring (in :mod:`src.main` /
    :mod:`src.supervisor`) calls :func:`default_registry` to populate
    the in-tree set.
    """

    def __init__(self, platforms: dict[str, type[Platform]]):
        self._platforms = dict(platforms)

    def get(self, name: str) -> type[Platform] | None:
        return self._platforms.get(name)

    def names(self) -> list[str]:
        return list(self._platforms.keys())

    def create(
        self,
        name: str,
        profile: AgentProfile | None,
        llm_logger=None,
    ) -> Platform:
        cls = self._platforms.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown platform: {name!r}. Available: {sorted(self._platforms.keys())}"
            )
        return cls(profile=profile, llm_logger=llm_logger)


def default_registry() -> PlatformRegistry:
    """Return a :class:`PlatformRegistry` populated with all in-tree platforms.

    Imports of platform modules are lazy so test code can construct a
    bare registry without pulling in heavy SDK dependencies.
    """
    from src.platforms.claude_sdk import ClaudeSDKPlatform
    from src.platforms.claude_cli import ClaudeCLIPlatform
    from src.platforms.codex_cli import CodexCLIPlatform

    return PlatformRegistry(
        platforms={
            ClaudeSDKPlatform.name: ClaudeSDKPlatform,
            ClaudeCLIPlatform.name: ClaudeCLIPlatform,
            CodexCLIPlatform.name: CodexCLIPlatform,
        }
    )
```

Note: `default_registry()` won't yet import successfully because the platform modules don't exist. That's fine — Tasks 4–6 add them. Tests in `test_platforms_registry.py` only use the explicit-dict constructor and don't call `default_registry()`.

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_platforms_registry.py -v
```
Expected: all pass.

- [ ] **Step 5: Run ruff**

Run:
```bash
ruff check src/platforms/__init__.py tests/test_platforms_registry.py
```
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/platforms/__init__.py tests/test_platforms_registry.py
git commit -m "Add PlatformRegistry with default_registry() factory

The registry takes an explicit platforms dict so tests stay isolated.
default_registry() lazily imports the three in-tree platforms (added
in subsequent tasks) and is what production code wires up."
```

---

## Task 4: Move ClaudeAdapter to ClaudeSDKPlatform

This is a rename + small reshape (constructor takes `profile` instead of `config`). No behavior change; tests should still pass.

**Files:**
- Move: `src/adapters/claude.py` → `src/platforms/claude_sdk.py`
- Modify: `src/platforms/claude_sdk.py` (rename class, reshape constructor)
- Move: ABC-conformance and ClaudeAdapter L0/L1 tests from `tests/test_adapters.py` to `tests/test_platforms_claude_sdk.py`
- Modify: `tests/test_claude_usage.py` (update imports if it uses `ClaudeAdapter`)

- [ ] **Step 1: Move the source file**

Run:
```bash
git mv src/adapters/claude.py src/platforms/claude_sdk.py
```

- [ ] **Step 2: Update imports and class name in `src/platforms/claude_sdk.py`**

Edit:
- Replace `from src.adapters.base import AgentAdapter, MessageCallback` with `from src.platforms.base import Capability, MessageCallback, Platform`.
- Replace `class ClaudeAdapter(AgentAdapter):` with:
  ```python
  class ClaudeSDKPlatform(Platform):
      name = "claude_sdk"
      capabilities = frozenset(Capability)  # SDK supports the full feature surface
  ```
- Replace `__init__(self, config: ClaudeAdapterConfig | None = None, llm_logger=None)` with:
  ```python
  def __init__(self, profile=None, llm_logger=None):
      self._config = self._config_from_profile(profile)
      ...rest unchanged...
  ```
- Add a new `@staticmethod` (or top-level function in this module) `_config_from_profile(profile)` that returns a `ClaudeAdapterConfig`. The body is the same logic as the current `AdapterFactory._config_for_profile()` in `src/adapters/__init__.py`:
  ```python
  @staticmethod
  def _config_from_profile(profile) -> "ClaudeAdapterConfig":
      base = ClaudeAdapterConfig()
      if profile is None:
          return base
      return ClaudeAdapterConfig(
          model=profile.model or base.model,
          permission_mode=profile.permission_mode or base.permission_mode,
          allowed_tools=profile.allowed_tools or base.allowed_tools,
          max_turns=base.max_turns,
      )
  ```
- Rename the dataclass `ClaudeAdapterConfig` to keep the name (it's internal; renaming would force more file edits). Just leave it as `ClaudeAdapterConfig` inside the new module.
- Search for any other references to `ClaudeAdapter` inside the file body (e.g., in error messages or log strings) and rename to `ClaudeSDKPlatform`.

- [ ] **Step 3: Move ABC-related tests out of `tests/test_adapters.py`**

Create `tests/test_platforms_claude_sdk.py` containing the following content extracted from `tests/test_adapters.py`:

- The `TestClaudeAdapterL0L1Injection` class (lines 100–218 of the current `test_adapters.py`).
- Update its `_make_adapter` helper to import from the new path:
  ```python
  def _make_platform(self):
      from src.platforms.claude_sdk import ClaudeSDKPlatform
      return ClaudeSDKPlatform(profile=None)
  ```
  And rename `adapter` to `platform` throughout the class (keeps it readable; the platform's API is unchanged from the adapter's).

Leave `TestMockAdapter` and `TestTaskContextL0L1Fields` in `tests/test_adapters.py` for now — the next step deletes the rest of that file once the platform tests and the deletion of `src/adapters/` happen together.

Actually — `TestMockAdapter` is now stale (refers to `MockAdapter` which we replaced with `MockPlatform` in Task 1). Delete it. `TestTaskContextL0L1Fields` is about `TaskContext` itself, not about the adapter; move it to a more appropriate file in Task 11.

For this task, just create `tests/test_platforms_claude_sdk.py` with `TestClaudeAdapterL0L1Injection` (renamed to `TestClaudeSDKPlatformL0L1Injection`) and the necessary imports.

- [ ] **Step 4: Update `tests/test_claude_usage.py`**

Search for `from src.adapters.claude import ClaudeAdapter` and replace with `from src.platforms.claude_sdk import ClaudeSDKPlatform`. Search for usages of the class and update.

Run:
```bash
grep -nE "ClaudeAdapter|src\.adapters\.claude" tests/test_claude_usage.py
```
Update each match.

- [ ] **Step 5: Update any other importers**

Run:
```bash
grep -rn "from src\.adapters\.claude\|import src\.adapters\.claude" src/ tests/
```
For each result, update the import to `from src.platforms.claude_sdk import ClaudeSDKPlatform` and rename `ClaudeAdapter` references in code to `ClaudeSDKPlatform`. Don't touch `src/adapters/__init__.py` yet (it'll be deleted in Task 11).

Also check imports of the config dataclass:
```bash
grep -rn "ClaudeAdapterConfig" src/ tests/
```
Update those imports to `from src.platforms.claude_sdk import ClaudeAdapterConfig`.

- [ ] **Step 6: Run the SDK platform tests**

Run:
```bash
pytest tests/test_platforms_claude_sdk.py tests/test_claude_usage.py -v
```
Expected: all pass.

- [ ] **Step 7: Run the full test suite to catch missed import sites**

Run:
```bash
pytest tests/ -x --no-header 2>&1 | tail -30
```
Expected: any failures are missed import sites; fix them. Common failure mode: tests still importing from `src.adapters.claude` or referencing `ClaudeAdapter`. Re-run grep from Step 5 if surprises appear.

- [ ] **Step 8: Run ruff**

Run:
```bash
ruff check src/platforms/claude_sdk.py tests/test_platforms_claude_sdk.py
```
Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "Rename ClaudeAdapter -> ClaudeSDKPlatform, move to src/platforms/claude_sdk.py

Constructor now takes profile (was config); profile->config translation
moves into the platform itself via _config_from_profile, removing the
last Claude-specific branch from AdapterFactory's selection logic.

Behavior preserved verbatim — the body of wait()/start()/etc is
unchanged. Class declares name='claude_sdk' and capabilities=
frozenset(Capability) (SDK supports the full feature surface)."
```

---

## Task 5: ClaudeCLIPlatform

Wraps `claude -p --output-format stream-json` and produces the same kind of `AgentOutput` that `ClaudeSDKPlatform` does. Same capability set as the SDK in v1 (the SDK is a wrapper around this CLI).

**Files:**
- Create: `src/platforms/claude_cli.py`
- Create: `tests/test_platforms_claude_cli.py`

- [ ] **Step 1: Write failing happy-path test**

Create `tests/test_platforms_claude_cli.py`:
```python
"""Tests for ClaudeCLIPlatform.

Subprocess invocation is mocked via `_subprocess.run_streaming_subprocess`
so tests don't depend on the local `claude` CLI being authenticated.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from src.platforms.base import Capability
from src.platforms.claude_cli import ClaudeCLIPlatform
from src.models import AgentResult, TaskContext


def _make_task(**overrides) -> TaskContext:
    defaults = {
        "description": "Implement the foo feature",
        "task_id": "t-1",
        "checkout_path": "/tmp/test-workspace",
    }
    defaults.update(overrides)
    return TaskContext(**defaults)


class TestClaudeCLIPlatformContract:
    def test_name_and_capabilities(self):
        assert ClaudeCLIPlatform.name == "claude_cli"
        # Same surface as the SDK in v1.
        assert ClaudeCLIPlatform.capabilities == frozenset(Capability)

    @pytest.mark.asyncio
    async def test_lifecycle_basic(self):
        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        assert await platform.is_alive()
        await platform.stop()
        assert not await platform.is_alive()


def _ndjson_lines(*objs) -> list[bytes]:
    return [(json.dumps(o) + "\n").encode() for o in objs]


class TestClaudeCLIPlatformWait:
    @pytest.mark.asyncio
    async def test_happy_path_completed(self):
        """A successful run produces COMPLETED with summary + token count."""
        emitted_lines = _ndjson_lines(
            {"type": "system", "subtype": "init", "session_id": "s-1"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}},
            {
                "type": "result",
                "subtype": "success",
                "result": "Done.",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.COMPLETED
        assert "Done." in output.summary
        assert output.tokens_used == 150

    @pytest.mark.asyncio
    async def test_cancellation_returns_failed(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            await cancel_event.wait()
            return -15

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())

        async def cancel_soon():
            await asyncio.sleep(0.05)
            await platform.stop()

        asyncio.create_task(cancel_soon())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.FAILED
        assert "cancelled" in output.summary.lower() or "stopped" in (output.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_rate_limit_error_on_subprocess_stderr(self):
        # The stderr scan classifies "rate limit" patterns into PAUSED_RATE_LIMIT.
        emitted_lines = _ndjson_lines(
            {"type": "result", "subtype": "error", "result": "HTTP 429: rate limit exceeded"},
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 1

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.PAUSED_RATE_LIMIT

    @pytest.mark.asyncio
    async def test_token_quota_exhaustion(self):
        emitted_lines = _ndjson_lines(
            {"type": "result", "subtype": "error", "result": "token quota exceeded"},
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 1

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.PAUSED_TOKENS

    @pytest.mark.asyncio
    async def test_subprocess_crash_no_ndjson(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            return 137  # killed

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.FAILED

    @pytest.mark.asyncio
    async def test_streams_messages_via_callback(self):
        emitted_lines = _ndjson_lines(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Step 1"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Step 2"}]}},
            {"type": "result", "subtype": "success", "result": "Done.", "usage": {"input_tokens": 10, "output_tokens": 10}},
        )
        received: list[str] = []

        async def on_message(text: str) -> None:
            received.append(text)

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted_lines:
                on_line(line)
            return 0

        platform = ClaudeCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.claude_cli.run_streaming_subprocess", side_effect=fake_run):
            await platform.wait(on_message=on_message)

        # At least the two assistant messages reached the callback.
        joined = "\n".join(received)
        assert "Step 1" in joined
        assert "Step 2" in joined
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_platforms_claude_cli.py -v
```
Expected: ImportError on `src.platforms.claude_cli`.

- [ ] **Step 3: Implement skeleton**

Create `src/platforms/claude_cli.py`:
```python
"""Claude CLI platform — runs tasks via the `claude` CLI in -p stream-json mode.

Functionally equivalent to :class:`ClaudeSDKPlatform` (the SDK is a wrapper
around this CLI), but reaches the agent without the SDK as an intermediary.
This is the path of least resistance when the SDK lacks a feature the CLI
exposes.

Capabilities are identical to the SDK's full set in v1; the distinction
exists so future divergence (e.g., a tmux-backed LIVE_TAKEOVER cap) can
be added to one platform without affecting the other.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from typing import ClassVar

from src.logging_config import get_correlation_context
from src.models import AgentOutput, AgentResult, TaskContext
from src.platforms._subprocess import (
    isolated_env,
    parse_ndjson_line,
    run_streaming_subprocess,
)
from src.platforms.base import Capability, MessageCallback, Platform

logger = logging.getLogger(__name__)


def _classify_error_result(error_msg: str) -> AgentResult:
    """Classify a CLI error message into the appropriate AgentResult.

    Mirrors the ClaudeSDKPlatform classifier so behavior is consistent
    across the two Claude platforms.
    """
    lower = error_msg.lower()
    if "hit your limit" in lower or "resets " in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "rate_limit" in lower or "rate limit" in lower or "429" in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "overloaded" in lower or "503" in lower or "capacity" in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "token" in lower or "quota" in lower:
        return AgentResult.PAUSED_TOKENS
    return AgentResult.FAILED


class ClaudeCLIPlatform(Platform):
    """Platform that wraps the `claude -p --output-format stream-json` CLI."""

    name: ClassVar[str] = "claude_cli"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(Capability)

    def __init__(self, profile=None, llm_logger=None):
        self._profile = profile
        self._llm_logger = llm_logger
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._session_id: str | None = None
        # Accumulated NDJSON events; used to build summary + token count.
        self._events: list[dict] = []
        self._on_message: MessageCallback | None = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()
        self._events = []
        self._session_id = None
        ctx = get_correlation_context()
        logger.info(
            "ClaudeCLI platform starting for task %s",
            ctx.get("task_id", task.task_id if hasattr(task, "task_id") else "unknown"),
        )

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        assert self._task is not None, "wait() called before start()"
        self._on_message = on_message

        prompt = self._build_prompt()
        cmd = self._build_command()
        env = isolated_env()
        cwd = self._task.checkout_path or "."

        # Pump every emitted NDJSON line through the dispatcher.
        loop = asyncio.get_running_loop()

        def _on_line(line: bytes) -> None:
            event = parse_ndjson_line(line)
            if event is None:
                return
            self._events.append(event)
            # Schedule the (async) callback dispatcher on the running loop.
            asyncio.run_coroutine_threadsafe(self._dispatch(event), loop)

        # Send prompt via stdin (avoids argv length limits and quoting bugs).
        # Implementation detail: pass `--print -` and write the prompt to the
        # subprocess's stdin. For simplicity in v1, we use argv with a fenced
        # prompt; if argv-length limits bite, switch to stdin per spec.
        cmd_with_prompt = [*cmd, prompt]

        start_time = time.monotonic()
        try:
            exit_code = await run_streaming_subprocess(
                cmd=cmd_with_prompt,
                env=env,
                cwd=cwd,
                on_line=_on_line,
                cancel_event=self._cancel_event,
            )
        except Exception as e:
            logger.exception("ClaudeCLI subprocess failed")
            return self._build_failure_output(str(e))

        # Drain any pending dispatches scheduled via run_coroutine_threadsafe.
        await asyncio.sleep(0)

        if self._cancel_event.is_set():
            return AgentOutput(
                result=AgentResult.FAILED,
                summary="Cancelled",
                error_message="Agent was stopped",
            )

        result_event = self._final_result_event()
        if result_event is None:
            # No result event arrived — subprocess crashed mid-stream.
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"ClaudeCLI exited with code {exit_code} before emitting a result event",
            )

        subtype = result_event.get("subtype")
        if subtype == "success":
            text = result_event.get("result", "")
            usage = result_event.get("usage") or {}
            tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            output = AgentOutput(
                result=AgentResult.COMPLETED,
                summary=text,
                tokens_used=tokens,
            )
        else:
            text = result_event.get("result", "")
            output = AgentOutput(
                result=_classify_error_result(text),
                summary=text,
                error_message=text,
            )

        # Surface duration to logs for parity with ClaudeSDKPlatform.
        logger.info(
            "ClaudeCLI finished task %s in %.1fs (exit=%s, result=%s)",
            self._task.task_id,
            time.monotonic() - start_time,
            exit_code,
            output.result,
        )
        return output

    async def stop(self) -> None:
        self._cancel_event.set()

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

    # ---------------- helpers ----------------

    def _build_command(self) -> list[str]:
        """Assemble the `claude` invocation (without the trailing prompt)."""
        cli = shutil.which("claude")
        if cli is None:
            raise RuntimeError("`claude` CLI not found in PATH")
        cmd = [cli, "-p", "--output-format", "stream-json", "--verbose"]
        # Permission mode from profile, if set.
        if self._profile is not None:
            mode = getattr(self._profile, "permission_mode", "") or ""
            if mode:
                cmd += ["--permission-mode", mode]
            model = getattr(self._profile, "model", "") or ""
            if model:
                cmd += ["--model", model]
        return cmd

    def _build_prompt(self) -> str:
        """Assemble the agent prompt from TaskContext (mirrors ClaudeSDKPlatform)."""
        assert self._task is not None
        parts: list[str] = []
        if self._task.l0_role:
            parts.append(self._task.l0_role)
        if self._task.l1_facts:
            parts.append(self._task.l1_facts)
        parts.append(self._task.description)
        if self._task.acceptance_criteria:
            parts.append("## Acceptance Criteria")
            for c in self._task.acceptance_criteria:
                parts.append(f"- {c}")
        if self._task.test_commands:
            parts.append("## Test Commands")
            for cmd in self._task.test_commands:
                parts.append(f"- `{cmd}`")
        if self._task.attached_context:
            parts.append("## Additional Context")
            for ctx in self._task.attached_context:
                parts.append(f"- {ctx}")
        return "\n\n".join(p for p in parts if p)

    async def _dispatch(self, event: dict) -> None:
        """Dispatch an NDJSON event to the on_message callback as readable text."""
        if self._on_message is None:
            return
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            self._session_id = event.get("session_id")
            return
        if etype == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text:
                        await self._on_message(text)
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    await self._on_message(f"-# {name}")
        elif etype == "result":
            text = event.get("result", "")
            if text:
                await self._on_message(text)

    def _final_result_event(self) -> dict | None:
        for event in reversed(self._events):
            if event.get("type") == "result":
                return event
        return None

    def _build_failure_output(self, error: str) -> AgentOutput:
        return AgentOutput(
            result=_classify_error_result(error),
            summary=error,
            error_message=error,
        )
```

Note: tests use `patch("src.platforms.claude_cli.run_streaming_subprocess", ...)`, so we import the function at module level (not via the package alias) — see the import line in the file.

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_platforms_claude_cli.py -v
```
Expected: all pass. Common first-run issues:
- `_dispatch` is scheduled via `run_coroutine_threadsafe` but the test loop has already advanced past the await point. Add `await asyncio.sleep(0)` before reading callback results, or restructure `_on_line` to call the dispatch inline (synchronously) — pragmatic if tests prove flaky.
- The streaming-test asserts `_dispatch` was called. If it never fires, drop `run_coroutine_threadsafe` and call the async dispatch via `asyncio.create_task` from inside `wait()` instead.

- [ ] **Step 5: Run ruff**

Run:
```bash
ruff check src/platforms/claude_cli.py tests/test_platforms_claude_cli.py
```
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/platforms/claude_cli.py tests/test_platforms_claude_cli.py
git commit -m "Add ClaudeCLIPlatform wrapping 'claude -p --output-format stream-json'

Functionally equivalent to ClaudeSDKPlatform but reaches the CLI
without the SDK as an intermediary. Capability set identical in v1.
Mocks subprocess via _subprocess.run_streaming_subprocess so tests
don't depend on local claude CLI authentication."
```

---

## Task 6: CodexCLIPlatform

Wraps `codex exec --json` for non-interactive runs. The streaming format and exact flags are verified during this task by running `codex exec --help` and adjusting accordingly.

**Files:**
- Create: `src/platforms/codex_cli.py`
- Create: `tests/test_platforms_codex_cli.py`

- [ ] **Step 1: Verify codex CLI flags and JSON shape**

Run:
```bash
codex exec --help 2>&1 | grep -E "json|output|stream|format" | head -10
codex exec --json "Say hello" 2>&1 | head -30 || true
```

Note the exact JSON shape emitted (event field names like `type`, `delta`, `message`, etc.). The implementation below assumes `{"type": "...", ...}`-shaped events; if Codex uses a different schema, adapt the dispatcher accordingly. **Don't skip this step** — Codex's output schema isn't stable across versions.

If `codex exec --json` requires authentication that isn't set up, capture the help output and proceed with the schema described below as an assumption. The unit tests use mocked NDJSON, so the implementation can be developed without a working Codex auth.

- [ ] **Step 2: Write failing tests**

Create `tests/test_platforms_codex_cli.py`:
```python
"""Tests for CodexCLIPlatform.

Same mocking approach as ClaudeCLIPlatform — patch run_streaming_subprocess
so tests are independent of Codex authentication and CLI surface drift.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from src.platforms.base import Capability
from src.platforms.codex_cli import CodexCLIPlatform
from src.models import AgentResult, TaskContext


def _make_task(**overrides) -> TaskContext:
    defaults = {"description": "Implement foo", "task_id": "t-1", "checkout_path": "/tmp/x"}
    defaults.update(overrides)
    return TaskContext(**defaults)


def _ndjson_lines(*objs) -> list[bytes]:
    return [(json.dumps(o) + "\n").encode() for o in objs]


class TestCodexCLIPlatformContract:
    def test_name(self):
        assert CodexCLIPlatform.name == "codex_cli"

    def test_capabilities_includes_streaming_json(self):
        assert Capability.STREAMING_JSON in CodexCLIPlatform.capabilities

    def test_capabilities_excludes_skills_and_memory_md(self):
        # Codex doesn't share Claude's skills / MEMORY.md infrastructure.
        assert Capability.SKILLS not in CodexCLIPlatform.capabilities
        assert Capability.MEMORY_MD not in CodexCLIPlatform.capabilities

    @pytest.mark.asyncio
    async def test_lifecycle_basic(self):
        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        assert await platform.is_alive()
        await platform.stop()
        assert not await platform.is_alive()


class TestCodexCLIPlatformWait:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        emitted = _ndjson_lines(
            {"type": "message", "role": "assistant", "content": "Working on it"},
            {"type": "result", "status": "success", "summary": "Done", "tokens": {"input": 100, "output": 50}},
        )

        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            for line in emitted:
                on_line(line)
            return 0

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.COMPLETED
        assert "Done" in output.summary
        assert output.tokens_used == 150

    @pytest.mark.asyncio
    async def test_failed_exit_no_result(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            return 1

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()
        assert output.result == AgentResult.FAILED

    @pytest.mark.asyncio
    async def test_cancellation(self):
        async def fake_run(cmd, env, cwd, on_line, cancel_event, **kwargs):  # noqa: ARG001
            await cancel_event.wait()
            return -15

        platform = CodexCLIPlatform(profile=None)
        await platform.start(_make_task())

        async def cancel_soon():
            await asyncio.sleep(0.05)
            await platform.stop()

        asyncio.create_task(cancel_soon())
        with patch("src.platforms.codex_cli.run_streaming_subprocess", side_effect=fake_run):
            output = await platform.wait()

        assert output.result == AgentResult.FAILED
```

- [ ] **Step 3: Run tests to verify they fail**

Run:
```bash
pytest tests/test_platforms_codex_cli.py -v
```
Expected: ImportError.

- [ ] **Step 4: Implement `src/platforms/codex_cli.py`**

Create `src/platforms/codex_cli.py`:
```python
"""Codex CLI platform — runs tasks via the `codex exec --json` CLI.

Provisional capability set: STREAMING_JSON, RESUME, THINKING, MCP, PLAN_MODE.
The Codex CLI exposes `codex mcp` (so MCP belongs in the set) and a non-
interactive exec mode with JSON output.  The exact event shape emitted by
`codex exec --json` is observed during implementation (Step 1 of this
task) and the dispatcher adapted accordingly — assume `{"type": ..., ...}`
events here as the v1 baseline.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import ClassVar

from src.logging_config import get_correlation_context
from src.models import AgentOutput, AgentResult, TaskContext
from src.platforms._subprocess import (
    isolated_env,
    parse_ndjson_line,
    run_streaming_subprocess,
)
from src.platforms.base import Capability, MessageCallback, Platform

logger = logging.getLogger(__name__)


def _classify_error_result(error_msg: str) -> AgentResult:
    lower = error_msg.lower()
    if "rate limit" in lower or "429" in lower or "rate_limit" in lower:
        return AgentResult.PAUSED_RATE_LIMIT
    if "quota" in lower or "token limit" in lower:
        return AgentResult.PAUSED_TOKENS
    return AgentResult.FAILED


class CodexCLIPlatform(Platform):
    """Platform that wraps the `codex exec --json` CLI."""

    name: ClassVar[str] = "codex_cli"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({
        Capability.STREAMING_JSON,
        Capability.RESUME,
        Capability.THINKING,
        Capability.MCP,
        Capability.PLAN_MODE,
    })

    def __init__(self, profile=None, llm_logger=None):
        self._profile = profile
        self._llm_logger = llm_logger
        self._task: TaskContext | None = None
        self._cancel_event = asyncio.Event()
        self._events: list[dict] = []
        self._on_message: MessageCallback | None = None

    async def start(self, task: TaskContext) -> None:
        self._task = task
        self._cancel_event.clear()
        self._events = []
        ctx = get_correlation_context()
        logger.info(
            "CodexCLI platform starting for task %s",
            ctx.get("task_id", task.task_id if hasattr(task, "task_id") else "unknown"),
        )

    async def wait(self, on_message: MessageCallback | None = None) -> AgentOutput:
        assert self._task is not None
        self._on_message = on_message

        prompt = self._build_prompt()
        cmd = self._build_command(prompt)
        env = isolated_env()
        cwd = self._task.checkout_path or "."
        loop = asyncio.get_running_loop()

        def _on_line(line: bytes) -> None:
            event = parse_ndjson_line(line)
            if event is None:
                return
            self._events.append(event)
            asyncio.run_coroutine_threadsafe(self._dispatch(event), loop)

        start_time = time.monotonic()
        try:
            exit_code = await run_streaming_subprocess(
                cmd=cmd,
                env=env,
                cwd=cwd,
                on_line=_on_line,
                cancel_event=self._cancel_event,
            )
        except Exception as e:
            logger.exception("CodexCLI subprocess failed")
            return AgentOutput(
                result=_classify_error_result(str(e)),
                summary=str(e),
                error_message=str(e),
            )

        await asyncio.sleep(0)

        if self._cancel_event.is_set():
            return AgentOutput(
                result=AgentResult.FAILED,
                summary="Cancelled",
                error_message="Agent was stopped",
            )

        result_event = self._final_result_event()
        if result_event is None:
            return AgentOutput(
                result=AgentResult.FAILED,
                error_message=f"CodexCLI exited with code {exit_code} before emitting a result event",
            )

        if result_event.get("status") == "success":
            summary = result_event.get("summary", "")
            tokens = result_event.get("tokens") or {}
            tokens_used = int(tokens.get("input", 0)) + int(tokens.get("output", 0))
            output = AgentOutput(result=AgentResult.COMPLETED, summary=summary, tokens_used=tokens_used)
        else:
            text = result_event.get("error") or result_event.get("summary", "")
            output = AgentOutput(result=_classify_error_result(text), summary=text, error_message=text)

        logger.info(
            "CodexCLI finished task %s in %.1fs (exit=%s, result=%s)",
            self._task.task_id,
            time.monotonic() - start_time,
            exit_code,
            output.result,
        )
        return output

    async def stop(self) -> None:
        self._cancel_event.set()

    async def is_alive(self) -> bool:
        return self._task is not None and not self._cancel_event.is_set()

    # ---------------- helpers ----------------

    def _build_command(self, prompt: str) -> list[str]:
        cli = shutil.which("codex")
        if cli is None:
            raise RuntimeError("`codex` CLI not found in PATH")
        cmd = [cli, "exec", "--json"]
        if self._profile is not None:
            model = getattr(self._profile, "model", "") or ""
            if model:
                cmd += ["--model", model]
        # Pass the prompt as the trailing positional argument.
        cmd += [prompt]
        return cmd

    def _build_prompt(self) -> str:
        assert self._task is not None
        parts: list[str] = []
        if self._task.l0_role:
            parts.append(self._task.l0_role)
        if self._task.l1_facts:
            parts.append(self._task.l1_facts)
        parts.append(self._task.description)
        if self._task.acceptance_criteria:
            parts.append("## Acceptance Criteria")
            for c in self._task.acceptance_criteria:
                parts.append(f"- {c}")
        return "\n\n".join(p for p in parts if p)

    async def _dispatch(self, event: dict) -> None:
        if self._on_message is None:
            return
        etype = event.get("type")
        if etype == "message":
            text = event.get("content", "") or ""
            if text:
                await self._on_message(text)
        elif etype == "tool_call":
            name = event.get("name", "?")
            await self._on_message(f"-# {name}")

    def _final_result_event(self) -> dict | None:
        for event in reversed(self._events):
            if event.get("type") == "result":
                return event
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
pytest tests/test_platforms_codex_cli.py -v
```
Expected: all pass.

- [ ] **Step 6: Run ruff**

Run:
```bash
ruff check src/platforms/codex_cli.py tests/test_platforms_codex_cli.py
```
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/platforms/codex_cli.py tests/test_platforms_codex_cli.py
git commit -m "Add CodexCLIPlatform wrapping 'codex exec --json'

Provisional capability set: STREAMING_JSON, RESUME, THINKING, MCP,
PLAN_MODE. Event-shape dispatcher assumes {type, content, ...}-keyed
events; verify against codex's actual output during integration."
```

---

## Task 7: Add `config.default_platform` field

This is the temporary stub that lets phase 1 ship without phase 2's profile-driven dispatch. Phase 2 deletes this field.

**Files:**
- Modify: `src/config.py`
- Modify: `tests/test_config.py` (or wherever AppConfig validation tests live)

- [ ] **Step 1: Find the AppConfig validation site**

Run:
```bash
grep -nE "class AppConfig|def validate" src/config.py | head
```
Note the line numbers for `AppConfig` and its `validate()` method.

- [ ] **Step 2: Add the field with validation**

Edit `src/config.py`. Inside the `AppConfig` dataclass, add after the existing fields (sort order following the file's convention — typically near the bottom):
```python
    # Phase-1 stub: which platform to spawn for tasks. Replaced by
    # profile.platform in phase 2 of the platforms refactor.
    default_platform: str = "claude_sdk"
```

In `AppConfig.validate()`, append a check:
```python
        # default_platform must be a known platform name.
        from src.platforms import default_registry

        try:
            available = default_registry().names()
        except Exception:  # pragma: no cover - registry import fail = bigger problem
            available = ["claude_sdk", "claude_cli", "codex_cli"]
        if self.default_platform not in available:
            errors.append(
                ConfigError(
                    section="app",
                    field="default_platform",
                    message=f"unknown platform {self.default_platform!r}; available: {available}",
                )
            )
```

- [ ] **Step 3: Find or create the config test file**

Run:
```bash
ls tests/test_config*.py 2>/dev/null
```
If a file exists, edit it; otherwise create `tests/test_config_default_platform.py`.

- [ ] **Step 4: Add tests for the new field**

Add to the chosen test file:
```python
def test_default_platform_default_value():
    from src.config import AppConfig
    cfg = AppConfig()
    assert cfg.default_platform == "claude_sdk"


def test_default_platform_validation_accepts_known():
    from src.config import AppConfig
    for name in ("claude_sdk", "claude_cli", "codex_cli"):
        cfg = AppConfig()
        cfg.default_platform = name
        errors = [e for e in cfg.validate() if e.field == "default_platform"]
        assert errors == [], f"{name} flagged as invalid"


def test_default_platform_validation_rejects_unknown():
    from src.config import AppConfig
    cfg = AppConfig()
    cfg.default_platform = "made-up"
    errors = [e for e in cfg.validate() if e.field == "default_platform"]
    assert len(errors) == 1
    assert "unknown platform" in errors[0].message.lower()
```

- [ ] **Step 5: Run the new tests**

Run:
```bash
pytest tests/test_config_default_platform.py -v
```
(or whatever file you used)
Expected: all pass.

- [ ] **Step 6: Run the full config test suite**

Run:
```bash
pytest tests/test_config*.py -v
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/config.py tests/test_config*.py
git commit -m "Add AppConfig.default_platform with validation against PlatformRegistry

Phase-1 stub: lets operators select which platform the orchestrator
spawns. Validated at config load against default_registry().names()
so misconfiguration fails fast at startup. Phase 2 deletes this field
once profile.platform drives dispatch."
```

---

## Task 8: Switch orchestrator call sites to use `PlatformRegistry`

**Files:**
- Modify: `src/orchestrator/core.py` (rename `_adapter_factory` → `_platforms`, update docstrings)
- Modify: `src/orchestrator/execution.py` (line ~411)
- Modify: `src/orchestrator/sync_workflow.py` (line ~273)

- [ ] **Step 1: Locate the orchestrator init parameter**

Run:
```bash
grep -nE "_adapter_factory|adapter_factory" src/orchestrator/core.py | head -20
```
Expected: hits in `__init__`, attribute assignments, and docstrings.

- [ ] **Step 2: Rename the parameter and attribute in core.py**

Edit `src/orchestrator/core.py`:
- Change `def __init__(self, config: AppConfig, adapter_factory=None):` to `def __init__(self, config: AppConfig, platforms=None):`
- Change `self._adapter_factory = adapter_factory` to `self._platforms = platforms`
- Update the docstring lines (around 195–220) to refer to "platforms registry" instead of "adapter factory".
- Update any `adapter_factory` references later in the file (search the file with grep).

- [ ] **Step 3: Update `src/orchestrator/execution.py`**

In `src/orchestrator/execution.py`, locate the existing call site (around line 411). Replace:
```python
adapter = self._adapter_factory.create("claude", profile=profile)
```
With:
```python
platform_name = self._config.default_platform
adapter = self._platforms.create(platform_name, profile=profile)
```

Also update any other references to `self._adapter_factory` in the file (e.g., around line 249 the "no adapter factory configured" guard) — replace `_adapter_factory` with `_platforms`, change error messages from "adapter factory" → "platforms registry".

- [ ] **Step 4: Update `src/orchestrator/sync_workflow.py`**

Same change as Step 3 for the line-273 call site:
```python
platform_name = self._config.default_platform
adapter = self._platforms.create(platform_name, profile=profile)
```
And rename `_adapter_factory` → `_platforms` throughout the file.

- [ ] **Step 5: Update any remaining `_adapter_factory` references**

Run:
```bash
grep -rn "_adapter_factory\|adapter_factory" src/orchestrator/ src/main.py src/supervisor.py
```
Update each.

- [ ] **Step 6: Run orchestrator tests**

Run:
```bash
pytest tests/test_orchestrator*.py -v 2>&1 | tail -30
```
Expected: all pass. If any test mocks `_adapter_factory`, update the mock to use `_platforms`.

- [ ] **Step 7: Commit**

```bash
git add src/orchestrator/ src/main.py src/supervisor.py
git commit -m "Rename Orchestrator._adapter_factory -> _platforms; use config.default_platform

Both call sites (execution.py:411, sync_workflow.py:273) now resolve
the platform name from config.default_platform instead of hardcoding
'claude'. Phase 2 will replace this with profile.platform."
```

---

## Task 9: Wire `PlatformRegistry` into `main.py` and `supervisor.py`

**Files:**
- Modify: `src/main.py`
- Modify: `src/supervisor.py` (if it constructs `AdapterFactory`)

- [ ] **Step 1: Find current AdapterFactory wiring**

Run:
```bash
grep -nE "AdapterFactory|adapter_factory" src/main.py src/supervisor.py
```
Note each instance.

- [ ] **Step 2: Replace in `src/main.py`**

In `src/main.py`, around lines 33 and 78–80:
- Change `from src.adapters import AdapterFactory` to `from src.platforms import default_registry`.
- Change:
  ```python
  orch = Orchestrator(config, adapter_factory=None)
  adapter_factory = AdapterFactory(llm_logger=orch.llm_logger)
  orch._adapter_factory = adapter_factory
  ```
  To:
  ```python
  orch = Orchestrator(config, platforms=None)
  registry = default_registry()
  orch._platforms = registry
  ```
  (The `llm_logger` was previously held inside `AdapterFactory`; now each `Platform` instance receives it via `registry.create(..., llm_logger=...)`. The orchestrator already passes `llm_logger` at the call site — verify with grep below.)

- [ ] **Step 3: Update orchestrator call sites to pass llm_logger**

Run:
```bash
grep -nE "_platforms\.create" src/orchestrator/*.py
```
For each match, ensure `llm_logger=self.llm_logger` (or whatever attribute holds it on Orchestrator) is passed:
```python
adapter = self._platforms.create(platform_name, profile=profile, llm_logger=self.llm_logger)
```

- [ ] **Step 4: Replace in `src/supervisor.py`**

Run:
```bash
grep -nE "AdapterFactory|adapter_factory" src/supervisor.py
```
For each match, apply the same pattern as `main.py`. If supervisor doesn't construct `AdapterFactory` directly, this step is a no-op.

- [ ] **Step 5: Run main + supervisor tests**

Run:
```bash
pytest tests/test_supervisor*.py tests/test_orchestrator*.py -v 2>&1 | tail -30
```
Expected: all pass.

- [ ] **Step 6: Smoke-run the daemon**

Run:
```bash
./run.sh start 2>&1 | head -30
```
(Or whatever the project's daemon-start script is.)
Expected: daemon starts without import errors. If it fails, the most likely cause is a missed reference to `AdapterFactory`. Stop the daemon (`./run.sh stop`).

- [ ] **Step 7: Commit**

```bash
git add src/main.py src/supervisor.py src/orchestrator/
git commit -m "Wire PlatformRegistry via default_registry() in main and supervisor

Replaces AdapterFactory construction. Orchestrator receives the
registry as 'platforms='; llm_logger is now passed per-create call
rather than held by the factory."
```

---

## Task 10: Move adapter spec docs to platforms

**Files:**
- Move: `docs/specs/adapters/` → `docs/specs/platforms/`
- Modify: every file in the moved directory (rename adapter → platform terminology)

- [ ] **Step 1: Move the directory**

Run:
```bash
git mv docs/specs/adapters docs/specs/platforms
```

- [ ] **Step 2: Rename `claude.md` to `claude_sdk.md`**

Run:
```bash
git mv docs/specs/platforms/claude.md docs/specs/platforms/claude_sdk.md
```

- [ ] **Step 3: Replace adapter terminology in moved files**

Each occurrence inside the moved files needs updating. Run a guided replacement:
```bash
grep -lE "AgentAdapter|adapter|Adapter" docs/specs/platforms/*.md
```
For each file, replace:
- `AgentAdapter` → `Platform`
- `adapter factory` → `platforms registry`
- `AdapterFactory` → `PlatformRegistry`
- `adapter module` / `adapter layer` → `platform module` / `platforms layer`
- Code-block file paths `src/adapters/...` → `src/platforms/...`
- Code-block class refs `ClaudeAdapter` → `ClaudeSDKPlatform`

Hand-edit rather than blanket sed: the noun "adapter" still appears in non-platform contexts (e.g., "Discord adapter") that we're not renaming.

- [ ] **Step 4: Add platform-specific spec stubs**

Create `docs/specs/platforms/claude_cli.md` and `docs/specs/platforms/codex_cli.md` with a minimal stub:
```markdown
---
tags: [spec, platforms, claude_cli]
---

# Claude CLI Platform

Wraps `claude -p --output-format stream-json` via subprocess. Functionally
equivalent to ClaudeSDKPlatform in v1.

See implementation at `src/platforms/claude_cli.py` and shared subprocess
helpers at `src/platforms/_subprocess.py`. Capability set is
`frozenset(Capability)` — same as the SDK platform.

(Detailed behavioral spec to be expanded as the platform matures.)
```

- [ ] **Step 5: Update CLAUDE.md references**

Run:
```bash
grep -nE "adapters|adapter" CLAUDE.md
```
Replace project-internal "adapters" references with "platforms" where appropriate. Leave subsystem mentions like "src/adapters/" → "src/platforms/" updated, but don't touch unrelated nouns.

- [ ] **Step 6: Update the "Quick Reference" subsystems list in CLAUDE.md**

Locate the line in CLAUDE.md mentioning `src/adapters/` and update to `src/platforms/`. Do the same for the file structure block at the top.

- [ ] **Step 7: Commit**

```bash
git add docs/specs/platforms/ CLAUDE.md
git rm -rf docs/specs/adapters/ 2>/dev/null || true
git commit -m "Move adapter specs to docs/specs/platforms; update CLAUDE.md

Renames AgentAdapter -> Platform terminology in spec files; adds
stub specs for the two new platforms (claude_cli, codex_cli)."
```

---

## Task 11: Delete `src/adapters/` and run the full suite

**Files:**
- Delete: `src/adapters/`
- Delete or refactor: `tests/test_adapters.py` (remaining content moves out)

- [ ] **Step 1: Final check for adapter imports**

Run:
```bash
grep -rn "from src\.adapters\|import src\.adapters" src/ tests/
```
Expected: ZERO results. Any hits are missed sites; fix them now (likely in test fixtures or older test files).

- [ ] **Step 2: Move remaining `tests/test_adapters.py` content**

The current `tests/test_adapters.py` has three test classes (after Task 4 removed the L0/L1 ones):
- `TestMockAdapter` — STALE, delete (replaced by `TestMockPlatform` in `tests/test_platforms_base.py`).
- `TestTaskContextL0L1Fields` — about `TaskContext`, not adapters. Move to `tests/test_models.py` (create if doesn't exist) or to `tests/test_task_context.py`.

Run:
```bash
ls tests/test_models.py tests/test_task_context.py 2>/dev/null
```
If neither exists, create `tests/test_task_context.py` with the `TestTaskContextL0L1Fields` content. Otherwise append to whichever exists.

- [ ] **Step 3: Delete `src/adapters/` and `tests/test_adapters.py`**

Run:
```bash
git rm -r src/adapters/
git rm tests/test_adapters.py
```

- [ ] **Step 4: Run the full test suite**

Run:
```bash
pytest tests/ -n auto 2>&1 | tail -30
```
Expected: all pass. Common last-mile failures:
- A test fixture referencing `MockAdapter` from `test_adapters.py` — move the import to `from tests.test_platforms_base import MockPlatform`.
- A doc test or skill test referencing the old class name.

- [ ] **Step 5: Run ruff on the full project**

Run:
```bash
ruff check src/ tests/
```
Expected: no errors related to the rename.

- [ ] **Step 6: Smoke-test the daemon end-to-end**

Run:
```bash
./run.sh start
sleep 3
# Check daemon log for import errors
tail -30 ~/.agent-queue/daemon.log 2>/dev/null | head -30
./run.sh stop
```
Expected: daemon starts cleanly; no `ModuleNotFoundError` or `ImportError`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Delete src/adapters/; move TaskContext tests out of test_adapters.py

src/adapters/ is fully replaced by src/platforms/. The MockAdapter
test class is replaced by MockPlatform in test_platforms_base.py.
TaskContext L0/L1 field tests move to test_task_context.py since
they were never adapter-specific."
```

---

## Verification Checklist

After Task 11, before declaring phase 1 done:

- [ ] `pytest tests/ -n auto` is green
- [ ] `ruff check src/ tests/` is clean
- [ ] `grep -r "AgentAdapter\|AdapterFactory\|src\.adapters\." src/ tests/` returns zero results
- [ ] `./run.sh start && sleep 3 && ./run.sh stop` completes without errors
- [ ] Manual test (optional, requires `~/.agent-queue/config.yaml` edit):
  - Set `default_platform: claude_cli` in config
  - Restart daemon
  - Create a trivial task
  - Confirm `claude -p` is invoked and the task completes
  - Repeat for `default_platform: codex_cli` (skip if Codex auth not configured locally)
- [ ] Commit history shows ~11 focused commits, one per task

## Out of scope (handed to phase 2)

Captured in the spec at `docs/superpowers/specs/2026-04-25-profile-validation-design.md`:
- Profile schema additions (`platform`, `role`, `rules`, `reflection`, `status`, `status_messages`, `last_validated_at`).
- The `## Platform` markdown section parser hook.
- Capability audit at sync time + `filter_for_platform()` runtime stripping.
- Discord surfacing of profile load issues.
- Strict task-creation gate (NOT NULL `profile_id`, name resolution, status checks).
- Removal of `config.default_platform`.
- Migration deleting NULL-`profile_id` tasks.
