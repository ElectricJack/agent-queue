"""Data-driven startup-dialog dismissal.

Harness markdown declares :class:`~src.sessions.provider.DialogRule` rows;
this module runs them against a live pane.  The runner is provider-agnostic
on purpose — it sees two callables (capture text, send keys) so it can be
unit-tested without tmux and reused by any pane-shaped provider.

One shared budget
-----------------
The Gas City post-mortem's pitfall table is explicit: per-dialog budgets
serially exceed the start deadline.  :class:`DialogBudget` is therefore a
single deadline shared across *every* dismissal pass of one startup —
``_await_ready`` interleaves passes before and after the readiness wait,
and they all draw down the same clock.

Quarantine rules terminate
--------------------------
A rule with ``quarantine=True`` (the rate-limit dialog) is not a dismissal:
its keys answer *Stop*, and the outcome tells the caller to quarantine the
session instead of continuing startup.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from src.sessions.provider import DialogRule

logger = logging.getLogger(__name__)

__all__ = ["DialogBudget", "DialogOutcome", "run_dialog_dismissal"]

#: Pause after sending a rule's keys, letting the TUI repaint before the
#: next capture decides whether the dialog is gone.
_SETTLE_SECONDS = 0.3


class DialogBudget:
    """One wall-clock budget shared by every dismissal pass of a startup."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    def exhausted(self) -> bool:
        return self.remaining() <= 0


@dataclass
class DialogOutcome:
    """What one dismissal pass did."""

    #: Rule names that matched and had their keys sent, in order.
    fired: list[str] = field(default_factory=list)
    #: The quarantine rule that matched, if any — terminal for startup.
    quarantined: DialogRule | None = None
    #: True when the pass stopped because the shared budget ran out.
    budget_exhausted: bool = False


def _matches(rule: DialogRule, text: str) -> bool:
    if rule.is_regex:
        try:
            return re.search(rule.pattern, text) is not None
        except re.error:
            logger.warning("Dialog rule %r has an invalid regex — skipped", rule.name)
            return False
    return rule.pattern in text


async def run_dialog_dismissal(
    *,
    capture: Callable[[], Awaitable[str]],
    send_keys: Callable[[tuple[str, ...]], Awaitable[None]],
    dialogs: tuple[DialogRule, ...],
    budget: DialogBudget,
    fired: set[str],
) -> DialogOutcome:
    """Run one dismissal pass: capture, match, answer, repeat until quiet.

    *fired* is caller-owned state carried across passes so ``once`` rules
    fire at most once per startup, not once per pass.
    """
    outcome = DialogOutcome()
    if not dialogs:
        return outcome

    while True:
        if budget.exhausted():
            outcome.budget_exhausted = True
            return outcome

        text = await capture()
        matched: DialogRule | None = None
        for rule in dialogs:
            if rule.once and rule.name in fired:
                continue
            if _matches(rule, text):
                matched = rule
                break

        if matched is None:
            return outcome

        fired.add(matched.name)
        outcome.fired.append(matched.name)
        logger.info("Dialog %r matched — sending %r", matched.name, matched.keys)
        if matched.keys:
            await send_keys(matched.keys)

        if matched.quarantine:
            outcome.quarantined = matched
            return outcome

        await asyncio.sleep(min(_SETTLE_SECONDS, max(budget.remaining(), 0)))
