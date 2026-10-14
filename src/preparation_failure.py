"""One bounded, scrubbed description of a claim-preparation failure.

Every surface that reports a failed pool claim preparation -- the claim
response, the session's ``claim_failure_reason``, the ``pool.prepare_failed``
event, task metadata and the daemon log -- carries the description built here,
so they can never disagree and an exception without text is never recorded as
an empty reason (``str(TimeoutError()) == ""``).

The description states only what was observed: the preparation stage that was
running, the exception type and message of each link of the cause chain and,
for a link without text, the deepest ``src/`` frame it was raised at. It never
guesses a source fault (a missing directory, a lock owner) from a blank message.
"""

from __future__ import annotations

import re
import traceback
from dataclasses import dataclass
from pathlib import Path

from src.projects.github import scrub_secrets

MAX_REASON_CHARS = 2000
MAX_MESSAGE_CHARS = 500
MAX_CAUSES = 3

_SRC_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _SRC_ROOT.parent

_EXTRA_SCRUBBERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
            re.DOTALL,
        ),
        "*** private key ***",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "***"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
    (
        re.compile(
            r"(?i)\b([\w-]*(?:password|passwd|secret|api[_-]?key|access[_-]?key"
            r"|(?:access|auth|refresh|session|bearer|api|private)[_-]?token))"
            r"(\s*[=:]\s*)[^\s,;]+"
        ),
        r"\1\2***",
    ),
)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class PreparationFailure:
    """What preparation recorded about one failure; ``reason`` is never empty."""

    stage: str
    error_type: str
    reason: str
    causes: tuple[str, ...]

    def record(self) -> dict:
        return {
            "reason": self.reason,
            "error_type": self.error_type,
            "stage": self.stage,
            "causes": list(self.causes),
        }

    def event_fields(self) -> dict:
        return {"reason": self.reason, "error_type": self.error_type, "stage": self.stage}


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def safe_message(exc: BaseException) -> str:
    """The exception's text, scrubbed, whitespace-collapsed and bounded."""
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 - a broken __str__ must not fail the error path
        return ""
    return safe_text(text)


def safe_text(text: str) -> str:
    """Scrub and bound diagnostic text, including a workspace path."""
    text = scrub_secrets(text)
    for pattern, replacement in _EXTRA_SCRUBBERS:
        text = pattern.sub(replacement, text)
    return _bounded(_WHITESPACE.sub(" ", text).strip(), MAX_MESSAGE_CHARS)


def _type_name(exc: BaseException) -> str:
    return type(exc).__qualname__


def raised_at(exc: BaseException) -> str | None:
    """The deepest frame under ``src/`` the exception passed through, if any."""
    try:
        frames = traceback.extract_tb(exc.__traceback__)
    except Exception:  # noqa: BLE001
        return None
    for frame in reversed(frames):
        try:
            path = Path(frame.filename).resolve()
        except (OSError, ValueError):
            continue
        if path.is_relative_to(_SRC_ROOT):
            return f"{path.relative_to(_REPO_ROOT).as_posix()}:{frame.lineno} {frame.name}"
    if frames:
        last = frames[-1]
        return f"{Path(last.filename).name}:{last.lineno} {last.name}"
    return None


def _link(exc: BaseException) -> str:
    message = safe_message(exc)
    if message:
        return f"{_type_name(exc)}: {message}"
    where = raised_at(exc)
    suffix = f" at {where}" if where else ""
    return f"{_type_name(exc)} (no message){suffix}"


def _chain(exc: BaseException) -> list[tuple[str, BaseException]]:
    links: list[tuple[str, BaseException]] = []
    seen = {id(exc)}
    current = exc
    while len(links) < MAX_CAUSES:
        if current.__cause__ is not None:
            relation, current = "caused by", current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            relation, current = "during", current.__context__
        else:
            break
        if id(current) in seen:
            break
        seen.add(id(current))
        links.append((relation, current))
    return links


def describe_preparation_failure(exc: BaseException, stage: str) -> PreparationFailure:
    """Describe ``exc`` raised while preparation ``stage`` was running.

    The result is deterministic for the same failure (no timestamps or object
    ids), so identical refusals still count as identical.
    """
    stage = _WHITESPACE.sub("_", (stage or "").strip()) or "unknown_stage"
    causes = tuple(f"{relation} {_link(link)}" for relation, link in _chain(exc))
    reason = "; ".join([f"{stage}: {_link(exc)}", *causes])
    return PreparationFailure(
        stage=stage,
        error_type=_type_name(exc),
        reason=_bounded(reason, MAX_REASON_CHARS),
        causes=causes,
    )
