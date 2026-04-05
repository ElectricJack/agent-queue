"""LLM interaction logging for prompt optimization and debugging.

Writes JSONL entries to date-organized files under ``logs/llm/``.  Three log
streams are maintained:

- ``chat_provider.jsonl`` — every ``ChatProvider.create_message()`` call
  (chat bot, hooks, plan parsing, summarization).
- ``claude_agent.jsonl`` — Claude Code agent task sessions (one entry per
  ``adapter.wait()`` completion).
- ``prompt_analytics.jsonl`` — aggregated prompt metrics for optimization
  (token counts, response times, success rates by caller/model).

Log files are append-only JSONL (one JSON object per line).  Retention is
configurable; ``cleanup_old_logs()`` removes date directories older than
``retention_days``.

All writes are synchronous (single ``json.dumps`` + file append) which is
fast enough for the expected call volume and avoids async complexity.

Prompt optimization features:

- Token efficiency tracking (output tokens / input tokens ratio)
- Per-caller and per-model latency percentiles
- Error rate tracking by provider
- Prompt template fingerprinting for A/B comparison

See ``specs/llm-logger.md`` for the logging specification.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


class PromptAnalytics:
    """In-memory aggregator for prompt optimization metrics.

    Tracks per-caller, per-model statistics to help identify:
    - Which callers consume the most tokens
    - Which models have the best latency/quality trade-off
    - Error rates by provider for reliability monitoring

    Metrics are flushed to ``prompt_analytics.jsonl`` periodically via
    ``flush()``.  The aggregator is intentionally simple: counters and
    running sums, no histograms or percentiles (those can be computed
    from the raw JSONL if needed).
    """

    def __init__(self):
        self._stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "call_count": 0,
                "total_input_tokens_est": 0,
                "total_output_tokens_est": 0,
                "total_duration_ms": 0,
                "error_count": 0,
                "last_call": None,
            }
        )

    def record(
        self,
        caller: str,
        model: str,
        provider: str,
        input_tokens_est: int,
        output_tokens_est: int,
        duration_ms: int,
        error: bool = False,
    ) -> None:
        """Record a single LLM call for analytics."""
        key = f"{caller}:{provider}:{model}"
        stats = self._stats[key]
        stats["call_count"] += 1
        stats["total_input_tokens_est"] += input_tokens_est
        stats["total_output_tokens_est"] += output_tokens_est
        stats["total_duration_ms"] += duration_ms
        if error:
            stats["error_count"] += 1
        stats["last_call"] = datetime.now(timezone.utc).isoformat()

    def get_summary(self) -> dict[str, Any]:
        """Return current aggregated statistics."""
        return dict(self._stats)

    def reset(self) -> None:
        """Clear all accumulated stats."""
        self._stats.clear()


class LLMLogger:
    """Append-only JSONL logger for LLM interactions.

    Provides three levels of logging detail:
    1. **Full call logs** (chat_provider.jsonl, claude_agent.jsonl) — every
       LLM interaction with input/output summaries for debugging.
    2. **Per-task logs** (tasks/{task_id}.jsonl) — all LLM calls for a
       specific task, enabling task-level prompt analysis.
    3. **Analytics** (prompt_analytics.jsonl) — aggregated metrics for
       monitoring token spend and identifying optimization opportunities.
    """

    def __init__(self, base_dir: str = "logs/llm", enabled: bool = True, retention_days: int = 30):
        self._base_dir = base_dir
        self._enabled = enabled
        self._retention_days = retention_days
        self._analytics = PromptAnalytics()

    @property
    def analytics(self) -> PromptAnalytics:
        """Access the in-memory prompt analytics aggregator."""
        return self._analytics

    def log_chat_provider_call(
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
        """Log a ChatProvider.create_message() call.

        Captures the full request/response cycle for a chat provider call,
        including timing, input summary, output summary, and any errors.
        Also feeds data into the prompt analytics aggregator for token
        efficiency monitoring.
        """
        if not self._enabled:
            return

        # Extract tool names only (schemas are large and static)
        tool_names = [t.get("name", "") for t in tools] if tools else []

        # Build response summary
        resp_summary: dict[str, Any] = {}
        output_text_length = 0
        if response is not None:
            text_parts = getattr(response, "text_parts", [])
            resp_summary["text_parts"] = text_parts
            output_text_length = sum(len(p) for p in text_parts)
            tool_uses = getattr(response, "tool_uses", [])
            if tool_uses:
                resp_summary["tool_uses"] = [
                    {
                        "name": getattr(tu, "name", ""),
                        "input": getattr(tu, "input", {}),
                    }
                    for tu in tool_uses
                ]

        # Estimate token counts for analytics (rough: 1 token ≈ 4 chars)
        input_tokens_est = (
            len(system) + sum(len(str(m.get("content", ""))) for m in messages)
        ) // 4
        output_tokens_est = output_text_length // 4

        # Generate a prompt template fingerprint for A/B comparison
        # Uses first 100 chars of system prompt + tool names as template ID
        template_sig = f"{system[:100]}|{','.join(sorted(tool_names))}"
        prompt_fingerprint = hashlib.md5(template_sig.encode(), usedforsecurity=False).hexdigest()[
            :12
        ]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "caller": caller,
            "model": model,
            "provider": provider,
            "duration_ms": duration_ms,
            "prompt_fingerprint": prompt_fingerprint,
            "input": {
                "system": system,
                "messages": messages,
                "tool_names": tool_names,
                "max_tokens": max_tokens,
                "input_tokens_est": input_tokens_est,
            },
            "output": {
                **resp_summary,
                "output_tokens_est": output_tokens_est,
            },
            "error": error,
        }

        self._append("chat_provider.jsonl", entry)

        # Feed analytics aggregator
        self._analytics.record(
            caller=caller,
            model=model,
            provider=provider,
            input_tokens_est=input_tokens_est,
            output_tokens_est=output_tokens_est,
            duration_ms=duration_ms,
            error=error is not None,
        )

    def log_agent_session(
        self,
        *,
        task_id: str,
        session_id: str | None = None,
        model: str = "",
        prompt: str = "",
        config_summary: dict | None = None,
        output: Any = None,
        duration_ms: int = 0,
    ) -> None:
        """Log a Claude Code agent task session.

        Captures the full agent execution lifecycle for a single task:
        session ID, prompt summary, output summary, and timing.

        Args:
            task_id: The task this session belongs to.
            session_id: Optional unique session identifier.
            model: Model name used for the agent run.
            prompt: System prompt sent to the agent.
            config_summary: Optional dict of agent config parameters.
            output: Agent output object (``AgentOutput``).
            duration_ms: Wall-clock duration of the session.
        """
        if not self._enabled:
            return

        output_summary: dict[str, Any] = {}
        if output is not None:
            output_summary["result"] = str(getattr(output, "result", ""))
            output_summary["summary"] = getattr(output, "summary", "")
            output_summary["tokens_used"] = getattr(output, "tokens_used", 0)
            output_summary["files_changed"] = getattr(output, "files_changed", [])
            error_msg = getattr(output, "error_message", "")
            if error_msg:
                output_summary["error"] = error_msg[:500]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "task_id": task_id,
            "session_id": session_id,
            "model": model,
            "duration_ms": duration_ms,
            "input": {
                "prompt_length": len(prompt),
                "prompt": prompt,
                **(config_summary or {}),
            },
            "output": output_summary,
        }

        self._append("claude_agent.jsonl", entry)
        # Also write to per-task log file if task_id is provided
        if task_id:
            self._append(f"tasks/{task_id}.jsonl", entry)

    def flush_analytics(self) -> None:
        """Write current analytics summary to prompt_analytics.jsonl and reset.

        Called periodically by the orchestrator (e.g. once per hour) to persist
        aggregated metrics without losing data on restart. The analytics entry
        includes per-caller/model breakdowns useful for prompt optimization.
        """
        if not self._enabled:
            return
        summary = self._analytics.get_summary()
        if not summary:
            return

        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "period_type": "flush",
            "callers": {},
        }
        for key, stats in summary.items():
            entry["callers"][key] = {
                **stats,
                "avg_duration_ms": (
                    stats["total_duration_ms"] / stats["call_count"]
                    if stats["call_count"] > 0
                    else 0
                ),
                "token_efficiency": (
                    stats["total_output_tokens_est"] / stats["total_input_tokens_est"]
                    if stats["total_input_tokens_est"] > 0
                    else 0
                ),
                "error_rate": (
                    stats["error_count"] / stats["call_count"] if stats["call_count"] > 0 else 0
                ),
            }

        self._append("prompt_analytics.jsonl", entry)
        self._analytics.reset()

    def get_analytics_summary(self) -> dict[str, Any]:
        """Return current in-memory analytics without flushing.

        Useful for health check endpoints and monitoring dashboards.
        """
        return self._analytics.get_summary()

    def cleanup_old_logs(self) -> int:
        """Delete date directories older than retention_days.

        Returns the number of directories removed.
        """
        if not os.path.isdir(self._base_dir):
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        removed = 0

        for name in os.listdir(self._base_dir):
            dir_path = os.path.join(self._base_dir, name)
            if not os.path.isdir(dir_path):
                continue
            # Only consider directories that look like date folders (YYYY-MM-DD)
            if len(name) == 10 and name < cutoff_str:
                try:
                    shutil.rmtree(dir_path)
                    removed += 1
                except OSError as e:
                    logger.warning("LLMLogger: failed to remove %s: %s", dir_path, e)

        return removed

    def _append(self, filename: str, entry: dict) -> None:
        """Append a single JSON line to a date-organized file."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dir_path = os.path.join(self._base_dir, today)

        file_path = os.path.join(dir_path, filename)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"

        with open(file_path, "a", encoding="utf-8") as f:
            f.write(line)

    @staticmethod
    def _summarize_messages(messages: list[dict]) -> list[dict]:
        """Create a compact summary of messages for logging.

        Captures role and content length/preview rather than full content
        to keep log entries reasonable in size.
        """
        summaries = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                summaries.append(
                    {
                        "role": role,
                        "content_length": len(content),
                        "content_preview": content[:150],
                    }
                )
            elif isinstance(content, list):
                # Tool results or multi-part content
                summaries.append(
                    {
                        "role": role,
                        "content_type": "list",
                        "content_count": len(content),
                    }
                )
            else:
                summaries.append(
                    {
                        "role": role,
                        "content_type": type(content).__name__,
                    }
                )
        return summaries
