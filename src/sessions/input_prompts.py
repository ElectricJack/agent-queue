"""Detect idle pool sessions parked on interactive harness prompts.

The signatures live in harness markdown (``input_prompts``), not in this
module.  Detection is deliberately observation-only: callers may report a
matching prompt, but must never choose a menu item or accept a trust prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Iterable

from src.models import SessionRecord
from src.pool_claims import idle_pool_claim_loop_stalled
from src.sessions.provider import Cap, SessionHandle

logger = logging.getLogger(__name__)

INPUT_PROMPT_PEEK_LINES = 60
INPUT_PROMPT_TAIL_LINES = 24


@dataclass(frozen=True)
class InputPromptSignature:
    """One named pane signature loaded from a harness markdown file."""

    name: str
    pattern: str
    is_regex: bool = False


@dataclass(frozen=True)
class AwaitingInput:
    """A stable, unclaimed pool session whose pane matches a signature."""

    session: SessionRecord
    signature: InputPromptSignature
    unchanged_seconds: float


def match_input_prompt(
    signatures: tuple[InputPromptSignature, ...], pane: str
) -> InputPromptSignature | None:
    """Return the first signature matching the non-empty tail of *pane*.

    Matching only the tail keeps an answered prompt left in scrollback from
    marking a healthy composer as blocked.  Regexes are case-insensitive,
    multiline and dot-all so a harness can describe a menu spanning lines.
    """
    if not pane:
        return None
    tail = "\n".join(
        line for line in pane.splitlines()[-INPUT_PROMPT_TAIL_LINES:] if line.strip()
    )
    folded = tail.casefold()
    for signature in signatures:
        if signature.is_regex:
            try:
                matched = re.search(
                    signature.pattern,
                    tail,
                    flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
                )
            except re.error:
                logger.warning(
                    "Input prompt signature %r has an invalid regex — skipped",
                    signature.name,
                )
                continue
            if matched is not None:
                return signature
        elif signature.pattern.casefold() in folded:
            return signature
    return None


async def find_awaiting_input_sessions(
    sessions: Iterable[SessionRecord],
    *,
    now: float,
    stall_seconds: float,
    harness_registry,
    providers,
    config,
) -> list[AwaitingInput]:
    """Inspect stable, unclaimed pool sessions and return prompt matches.

    The inactivity fence is the same one pool sizing uses for
    ``unresponsive`` supply.  A session with a task, a claim in flight, or
    recent pane/claim-loop activity is excluded before any provider call.
    Provider failures are unknown evidence and therefore produce no finding.
    """

    candidates: list[tuple[SessionRecord, tuple[InputPromptSignature, ...]]] = []
    for session in sessions:
        if (
            session.lifecycle != "pool"
            or session.state != "running"
            or session.desired_state != "running"
            or session.task_id is not None
            or session.claim_phase is not None
            or not idle_pool_claim_loop_stalled(
                session, now=now, stall_seconds=stall_seconds
            )
        ):
            continue
        harness = harness_registry.get(session.harness, session.project_id)
        signatures = tuple(getattr(harness, "input_prompts", ()) or ()) if harness else ()
        if signatures:
            candidates.append((session, signatures))

    async def inspect(
        session: SessionRecord, signatures: tuple[InputPromptSignature, ...]
    ) -> AwaitingInput | None:
        try:
            provider = providers.create(session.provider, config)
            if not provider.supports(Cap.PEEK):
                return None
            pane = await provider.peek(
                SessionHandle(
                    name=session.name,
                    provider=session.provider,
                    instance_token=session.instance_token,
                ),
                INPUT_PROMPT_PEEK_LINES,
            )
        except Exception:
            logger.debug(
                "input-prompt pane inspection failed for session %s",
                session.id,
                exc_info=True,
            )
            return None
        signature = match_input_prompt(signatures, pane)
        if signature is None:
            return None
        unchanged_since = session.last_activity or session.started_at
        return AwaitingInput(
            session=session,
            signature=signature,
            unchanged_seconds=max(0.0, now - unchanged_since),
        )

    inspected = await asyncio.gather(*(inspect(session, rules) for session, rules in candidates))
    return sorted(
        (finding for finding in inspected if finding is not None),
        key=lambda finding: finding.session.id,
    )
