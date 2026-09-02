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

Quiet windows catch late paints
-------------------------------
A pass that returns the moment one capture shows no dialog is racing the
TUI: Claude and Codex both paint their trust screen *after* the first
frames, so a pass that ran a beat early declared startup finished while
the harness was still blocked.  ``quiet_seconds`` makes a pass hold the
"no dialog" verdict for that long — re-arming the clock every time a rule
fires — so a dialog painted late is still answered.
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

__all__ = ["DialogBudget", "DialogOutcome", "first_match", "run_dialog_dismissal"]

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


def first_match(
    dialogs: tuple[DialogRule, ...], text: str, *, fired: set[str] | None = None
) -> DialogRule | None:
    """The first rule in *dialogs* whose pattern is on screen, if any.

    Shared with the readiness poll so "is a dialog covering the pane?" has
    exactly one answer.  Rules already in *fired* are skipped when they are
    ``once`` rules, matching the dismissal loop's own bookkeeping.
    """
    for rule in dialogs:
        if rule.once and fired is not None and rule.name in fired:
            continue
        if _matches(rule, text):
            return rule
    return None


async def run_dialog_dismissal(
    *,
    capture: Callable[[], Awaitable[str]],
    send_keys: Callable[[tuple[str, ...]], Awaitable[None]],
    dialogs: tuple[DialogRule, ...],
    budget: DialogBudget,
    fired: set[str],
    quiet_seconds: float = 0.0,
) -> DialogOutcome:
    """Run one dismissal pass: capture, match, answer, repeat until quiet.

    *fired* is caller-owned state carried across passes so ``once`` rules
    fire at most once per startup, not once per pass.

    With *quiet_seconds* > 0 the pass does not return on the first quiet
    capture: it keeps re-capturing until nothing has matched for that long,
    so a dialog the TUI paints a beat late is still answered.  The clock is
    re-armed whenever a rule fires, and the shared *budget* still bounds
    the whole thing.
    """
    outcome = DialogOutcome()
    if not dialogs:
        return outcome

    quiet_since: float | None = None
    while True:
        if budget.exhausted():
            outcome.budget_exhausted = True
            return outcome

        text = await capture()
        matched = first_match(dialogs, text, fired=fired)

        if matched is None:
            now = time.monotonic()
            if quiet_seconds <= 0:
                return outcome
            if quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= quiet_seconds:
                return outcome
            await asyncio.sleep(min(_SETTLE_SECONDS, max(budget.remaining(), 0)))
            continue

        quiet_since = None

        fired.add(matched.name)
        outcome.fired.append(matched.name)
        logger.info("Dialog %r matched — sending %r", matched.name, matched.keys)
        if matched.keys:
            await send_keys(matched.keys)

        if matched.quarantine:
            outcome.quarantined = matched
            return outcome

        await asyncio.sleep(min(_SETTLE_SECONDS, max(budget.remaining(), 0)))
