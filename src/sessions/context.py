"""Native worker context settings and bounded, read-only transcript observations.

No lifecycle actions or token/quota estimates. A request's observed input is a
context reading; cumulative spend and provider quota percentages are not.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from src.sessions.transcripts.base import parse_iso_ts

CONTEXT_TAIL_BYTES = 256 * 1024
CONTEXT_MAX_AGE_SECONDS = 300

#: Harnesses whose transcripts AQ reads for a context metric
#: (:func:`src.sessions.transcripts.resolve_reader`, consumed by
#: :func:`context_from_tail`) — the same two whose worker launches receive a
#: derived native compact setting (``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` in
#: ``src/sessions/spec.py``, ``model_auto_compact_token_limit`` via
#: :func:`codex_compact_override`).  Every other harness, including an
#: operator's ``opencode``, ``opencode-zen`` and the shipped ``gemini``, has
#: neither: guidance must not name a threshold AQ cannot read, nor a setting it
#: does not apply, to a model whose own window may be a fraction of it.
MEASURED_HARNESSES = frozenset({"claude", "codex"})


def measures_context(harness) -> bool:
    """Whether AQ can read *harness*'s context metric at all.

    False means a measured threshold is permanently unavailable for that
    harness, which is a different statement from "no fresh reading right now".
    """
    return str(harness or "") in MEASURED_HARNESSES


def compact_tokens(config) -> int:
    return getattr(getattr(config, "sessions", None), "worker_context_compact_tokens", 0)


def codex_compact_override(args: tuple[str, ...] | list[str], config) -> list[str]:
    """Explicit per-harness TOML overrides remain authoritative."""
    limit = compact_tokens(config)
    if not limit or any("model_auto_compact_token_limit" in arg for arg in args):
        return []
    return ["-c", f"model_auto_compact_token_limit={limit}"]


def context_from_tail(path: Path, harness: str, *, now: float) -> dict:
    result = {"input_tokens": None, "source": "unknown", "observed_at": None,
              "transcript_path": str(path)}
    with path.open("rb") as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - CONTEXT_TAIL_BYTES)
        stream.seek(start)
        data = stream.read(CONTEXT_TAIL_BYTES)
    lines = data.splitlines(keepends=True)
    if start and lines:
        lines.pop(0)  # May start inside a JSON record; never guess its prefix.
    for line in reversed(lines):
        if not line.endswith(b"\n"):
            continue  # In-flight record.
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                continue
            payload = raw.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            # A pre-compaction reading no longer describes the active context.
            if (raw.get("type") == "compacted"
                    or payload.get("type") == "context_compacted"
                    or raw.get("subtype") == "compact_boundary"):
                return result
            if harness == "claude":
                message = raw.get("message") or {}
                usage = message.get("usage") if isinstance(message, dict) else None
                if raw.get("type") != "assistant" or not isinstance(usage, dict):
                    continue
                keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                if not all(key in usage for key in keys):
                    return result
                values = [usage[key] for key in keys]
                source = "claude request input + cache creation + cache read"
            elif harness == "codex":
                if raw.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                usage = info.get("last_token_usage") if isinstance(info, dict) else None
                if not isinstance(usage, dict) or "input_tokens" not in usage:
                    return result
                values = [usage["input_tokens"]]
                source = "codex last request input (includes cache)"
            else:
                return result
            if any(type(value) is not int or value < 0 for value in values):
                return result
            ts = parse_iso_ts(raw.get("timestamp"))
            if not ts or not 0 <= now - ts <= CONTEXT_MAX_AGE_SECONDS:
                return result
            return {**result, "input_tokens": sum(values), "source": source, "observed_at": ts}
        except (ValueError, TypeError):
            continue
    return result


async def read_context(session, *, base_dir: Path | None = None, now: float | None = None) -> dict:
    unknown = {"input_tokens": None, "source": "unknown", "observed_at": None,
               "transcript_path": None}
    if session is None or not getattr(session, "session_key", None):
        return unknown
    from src.sessions.transcripts import resolve_reader

    reader = resolve_reader(session.harness, base_dir=base_dir)
    if reader is None:
        return unknown
    def read():
        try:
            path = reader.resolve_session(session)
            if path is None:
                return unknown
            return context_from_tail(path, session.harness, now=time.time() if now is None else now)
        except (OSError, ValueError, TypeError):
            return unknown
    return await asyncio.to_thread(read)


def unmeasured_harness_head(harness: str, cadence: int) -> tuple[str, str]:
    """Threshold and compaction sentences for a harness AQ cannot measure.

    No token number is named: the metric does not exist for this harness, so a
    threshold would be an unreachable instruction rather than a weak one. The
    cadence stays the fallback signal, and the actionable trigger is a boundary
    the worker can actually see — a finished change, or a step it expects to
    compact.
    """
    return (
        (
            f"{harness} reports no context metric AQ can read, so no measured threshold "
            f"applies here; do not infer one from spend. "
            f"AQ also applies no native compact limit to this harness; its own context "
            f"window and compaction do that. Checkpoint after each completed change and "
            f"before any step you expect to fill the remaining context, so a "
            f"provider-initiated compaction never costs finished work; otherwise "
            f"checkpoint at logical boundaries and at most every {cadence} tool turns. "
            f"This cadence is not a token estimate. "
        ),
        (
            "AQ sees no compaction signal for this harness, so the note you save is the "
            "explicit continuation record AQ can rely on: write it before the "
            "compaction, not after. "
        ),
    )


def context_guidance(config, session=None, observation: dict | None = None) -> str:
    settings = getattr(config, "sessions", None)
    checkpoint = getattr(settings, "worker_context_checkpoint_tokens", 120000)
    cadence = getattr(settings, "worker_context_unknown_checkpoint_turns", 40)
    harness = getattr(session, "harness", None)
    reading = (observation or {}).get("input_tokens")
    if harness and not measures_context(harness):
        # A reading cannot exist for a harness AQ cannot read; ignore one if a
        # caller supplies it rather than echoing back a number AQ cannot stand
        # behind.
        measured, native = unmeasured_harness_head(str(harness), cadence)
    else:
        measured = (
            f"Latest measured request input: {reading} tokens."
            if reading is not None
            else "Current context metric is unknown; do not infer it from spend."
        )
        trigger = (
            f"Save a continuation checkpoint at {checkpoint} measured input tokens. "
            if checkpoint else "Measured checkpoint threshold is disabled. "
        )
        limit = compact_tokens(config)
        trigger += (
            f"Future worker launches use a {limit}-token native compact setting. "
            if limit else "Native compact setting inherits harness defaults. "
        )
        if reading is not None and checkpoint and reading >= checkpoint:
            trigger += "The checkpoint threshold is reached: save the note before more work. "
        measured += "\n" + trigger
        measured += (
            f"When metrics are unknown, checkpoint at logical boundaries and at most every "
            f"{cadence} tool turns. This cadence is not a token estimate. "
        )
        native = (
            f"{harness} supports native /compact; allow its automatic compaction to run. "
            if harness in MEASURED_HARNESSES else
            "Use only the current harness's supported compaction mechanism. "
        )
    return (
        "Worker context checkpoints:\n" + measured
        + "Use `aq handoff --auto --goal ... --next-step ... --constraint ... --evidence ...` "
        "with exact checks, pending waits, evidence paths and approaches not to repeat. "
        + native
        + "Compaction keeps the task, claim and workspace. After compaction, run `aq prime` "
        "if the hook did not re-prime, check .aq/claim.json and the checkout, retrieve wait/job "
        "results by their existing IDs, and continue from the saved next action. "
        "Preserve human gates and all constraints; do not resubmit pending work. "
        "Save repetitive successful tool output to retrievable logs and return short summaries. "
        "Keep full error evidence and instructions; never truncate them to meet a summary budget. "
        "Do not split small tasks, clear the conversation, or restart a live worker for context."
    )
