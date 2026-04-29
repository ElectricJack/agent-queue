"""Tests for shared subprocess + NDJSON helpers used by CLI platforms."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from src.runtimes._subprocess import (
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
        import logging

        with caplog.at_level(logging.DEBUG, logger="src.runtimes._subprocess"):
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

    @pytest.mark.asyncio
    async def test_on_line_exception_does_not_abort_stream(self, tmp_path, caplog):
        """If on_line raises, the stream must continue and the error must be logged."""
        import logging

        script = tmp_path / "emit3.py"
        script.write_text(
            'import sys; sys.stdout.write(\'{"a":1}\\n{"b":2}\\n{"c":3}\\n\'); sys.stdout.flush()\n'
        )
        delivered: list[bytes] = []
        cancel = asyncio.Event()

        def callback(line: bytes) -> None:
            delivered.append(line)
            if line == b'{"b":2}\n':
                raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR, logger="src.runtimes._subprocess"):
            exit_code = await run_streaming_subprocess(
                cmd=["python3", str(script)],
                env=isolated_env(),
                cwd=str(tmp_path),
                on_line=callback,
                cancel_event=cancel,
            )

        assert exit_code == 0
        # All three lines were delivered despite the middle one's callback raising.
        assert delivered == [b'{"a":1}\n', b'{"b":2}\n', b'{"c":3}\n']
        # The exception was logged.
        assert any("boom" in r.message or "boom" in (r.exc_text or "") for r in caplog.records)
