"""One-shot, tool-free LLM calls through logged-in agent CLIs.

This path creates neither an AQ task nor a session. Each call runs in an empty
temporary directory, and the playbook executor validates its final JSON.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.llm.types import TokenUsage


@dataclass(frozen=True)
class CliAnswer:
    text: str
    usage: TokenUsage


def _usage(raw: dict | None) -> TokenUsage:
    raw = raw or {}
    input_tokens = raw.get("input_tokens", raw.get("prompt_tokens"))
    output_tokens = raw.get("output_tokens", raw.get("completion_tokens"))
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return TokenUsage()
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens, reported=True)


def _gemini_usage(stats: dict) -> TokenUsage:
    models = stats.get("models") or {}
    if not isinstance(models, dict) or not models:
        return TokenUsage()
    input_tokens = 0
    output_tokens = 0
    for model in models.values():
        tokens = model.get("tokens") if isinstance(model, dict) else None
        if not isinstance(tokens, dict):
            return TokenUsage()
        input_count = tokens.get("prompt", tokens.get("input"))
        total_count = tokens.get("total")
        if not isinstance(input_count, int) or not isinstance(total_count, int):
            return TokenUsage()
        if total_count < input_count:
            return TokenUsage()
        input_tokens += input_count
        output_tokens += total_count - input_count
    return TokenUsage(input_tokens, output_tokens, True)


def _answer(harness: str, stdout: str, output_file: Path) -> CliAnswer:
    if harness == "codex":
        text = output_file.read_text().strip()
        usage = TokenUsage()
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "turn.completed":
                usage = _usage(event.get("usage"))
        return CliAnswer(text, usage)
    data = json.loads(stdout)
    if harness == "claude":
        result = data.get("structured_output", data.get("result"))
        text = json.dumps(result) if isinstance(result, dict) else str(result or "")
        usage = data.get("usage") or {}
        parsed = _usage(usage)
        if parsed.reported:
            parsed = TokenUsage(
                parsed.input_tokens + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0),
                parsed.output_tokens,
                True,
            )
        return CliAnswer(text, parsed)
    if harness == "gemini":
        result = data.get("response", "")
        text = json.dumps(result) if isinstance(result, dict) else str(result)
        stats = data.get("stats") or {}
        return CliAnswer(text, _gemini_usage(stats))
    raise ValueError(f"unsupported CLI harness {harness!r}")


async def ask_cli(
    *, harness: str, model: str, effort: str, prompt: str, schema: dict,
    timeout_seconds: float, cancel_event: asyncio.Event | None = None,
) -> CliAnswer:
    """Ask once using the harness's own login; never expose AQ tools."""
    if harness not in {"codex", "claude", "gemini"} or not model:
        raise ValueError("CLI call needs a supported harness and model")
    with tempfile.TemporaryDirectory(prefix="aq-llm-") as directory:
        root = Path(directory)
        schema_path = root / "schema.json"
        schema_path.write_text(json.dumps(schema))
        output_path = root / "answer.json"
        if harness == "codex":
            args = [
                "codex", "exec", "--json", "--ephemeral", "--ignore-user-config",
                "--ignore-rules", "--skip-git-repo-check", "--sandbox", "read-only",
                "--output-schema", str(schema_path), "--output-last-message", str(output_path),
                "--model", model,
            ]
            if effort:
                args.extend(["--config", f'model_reasoning_effort="{effort}"'])
            args.append("-")
        elif harness == "claude":
            args = [
                "claude", "--print", "--no-session-persistence", "--tools", "",
                "--permission-mode", "dontAsk", "--output-format", "json",
                "--json-schema", json.dumps(schema), "--model", model,
            ]
            if effort and effort != "off":
                args.extend(["--effort", effort])
        else:
            policy_path = root / "deny-tools.toml"
            policy_path.write_text('[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n')
            settings_dir = root / ".gemini"
            settings_dir.mkdir()
            (settings_dir / "settings.json").write_text(json.dumps({
                "hooksConfig": {"enabled": False},
                "skills": {"enabled": False},
                "tools": {"core": []},
                "mcp": {"allowed": []},
            }))
            args = [
                "gemini", "--prompt", prompt, "--model", model,
                "--output-format", "json", "--approval-mode", "plan",
                "--skip-trust", "--policy", str(policy_path),
                "--extensions", "",
            ]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=root, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        communicate = asyncio.create_task(proc.communicate(prompt.encode() if harness != "gemini" else b""))
        cancellation = asyncio.create_task(cancel_event.wait()) if cancel_event else None
        try:
            waiters = {communicate}
            if cancellation is not None:
                waiters.add(cancellation)
            done, _ = await asyncio.wait(waiters, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
            if cancellation in done:
                raise asyncio.CancelledError
            if communicate not in done:
                raise TimeoutError("CLI LLM call timed out")
            stdout, stderr = communicate.result()
            if proc.returncode != 0:
                # CLI stderr may contain credential details; surface only a type.
                raise RuntimeError(f"{harness} CLI exited with status {proc.returncode}")
            answer = _answer(harness, stdout.decode(), output_path)
            if not answer.text:
                raise ValueError(f"{harness} CLI returned no answer")
            return answer
        finally:
            if cancellation:
                cancellation.cancel()
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            if not communicate.done():
                communicate.cancel()
