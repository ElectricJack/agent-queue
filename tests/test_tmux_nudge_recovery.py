"""Recovering a nudge whose Enter was lost to the composer's repaint.

Live failure (2026-09-02): the stall nudge for task ``stark-journey-63``
sat in its Claude composer, unsubmitted, until an operator ran one
``tmux send-keys Enter`` by hand.  The daemon knew — "pasted but not
submitted — will retry" — but the retry never happened: the text was left
in the composer, and every later nudge deferred on
``_require_empty_composer``.  A lost Enter therefore turned into a
permanent stall and the stall ladder stopped climbing.

The fake TUI here drops the first *N* Enters (what a pane being resized by
an attaching or detaching dashboard terminal does) and never touches a real
tmux server.
"""

from __future__ import annotations

import os

import pytest

if os.name != "posix":
    pytest.skip("tmux provider is POSIX-only", allow_module_level=True)

from src.sessions import tmux as tmux_module
from src.sessions.provider import NotSubmitted, NudgeDeferred
from src.sessions.tmux import _marker_for, _marker_on_input_line, _submit_pending
from tests.test_tmux_nudge_drafts import Composer, handle, provider_for

REMINDER = (
    "No progress for 8 min on task stark-journey-63. Close or continue: "
    'run `aq task close stark-journey-63 --outcome pass --summary "..."`.'
)
MARKER = _marker_for(REMINDER)

CLAUDE_LAYOUT = {
    "prefix": "❯ ",
    "row": "❯\N{NO-BREAK SPACE}",
    "above": ["older output", "────────────────────"],
    "below": ["────────────────────", "  bypass permissions on"],
}


class FlakyComposer(Composer):
    """A Claude-shaped composer that swallows its first *ignore_enters*.

    Everything else is inherited: an ignored Enter leaves the typed text on
    the input line exactly as the real repaint race does.
    """

    def __init__(self, *, ignore_enters=0, clear_keys=(), **kw):
        super().__init__(**{**CLAUDE_LAYOUT, **kw})
        self.ignore_enters = ignore_enters
        self.clear_keys = tuple(clear_keys)
        self.enters = 0
        self.clears = 0

    async def tmux(self, *args, stdin=None, **kwargs):
        command = args[0]
        if command == "show-environment":
            key = args[-1]
            values = {
                "AQ_READY_PREFIX": self.prefix,
                "AQ_SKIP_ESCAPE": "1",
                "AQ_CLEAR_KEYS": ",".join(self.clear_keys),
            }
            return f"{key}={values.get(key, '')}\n"
        if command == "send-keys" and args[-1] == "Enter":
            self.mutations.append(args)
            self.enters += 1
            if self.enters <= self.ignore_enters:
                return ""  # the repaint ate it; text stays in the composer
            self.submitted.append(self.draft)
            self._reset_input()
            return ""
        if command == "send-keys" and self.clear_keys and args[-1] in self.clear_keys:
            self.mutations.append(args)
            self.clears += 1
            self._reset_input()
            return ""
        return await super().tmux(*args, stdin=stdin, **kwargs)

    def _reset_input(self) -> None:
        self.draft = ""
        self.row = self.prefix
        self.typed = False


@pytest.fixture
def fast_polls(monkeypatch):
    """Keep the widening backoff's *shape* but not its wall-clock cost."""
    monkeypatch.setattr(tmux_module, "_SUBMIT_POLLS", ((0.0,),) * 4)


def typed_text(composer) -> list[str]:
    return [args[-1] for args in composer.mutations if "-l" in args]


class TestSubmitBackoff:
    def test_schedule_widens_and_keeps_a_fast_first_attempt(self):
        polls = tmux_module._SUBMIT_POLLS
        assert len(polls) >= 4, "one Enter attempt is not a recovery"
        assert sum(polls[0]) <= 1.0, "an idle harness must still confirm fast"
        assert [sum(p) for p in polls] == sorted(sum(p) for p in polls)
        assert max(polls[-1]) >= 1.0, "the last attempt must outlast a repaint storm"

    async def test_first_enter_swallowed_still_submits(self):
        composer = FlakyComposer(ignore_enters=1)
        await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.submitted == [REMINDER]
        assert composer.enters == 2

    async def test_repaint_storm_swallowing_three_enters_still_submits(self, fast_polls):
        composer = FlakyComposer(ignore_enters=3)
        provider = provider_for(composer)
        await provider.nudge(handle(), REMINDER)
        assert composer.submitted == [REMINDER]
        assert provider._unsubmitted == {}


class TestNeverLeaveTextBehind:
    async def test_clear_keys_empty_the_composer_when_enter_never_lands(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99, clear_keys=("C-u",))
        provider = provider_for(composer)

        with pytest.raises(NotSubmitted) as caught:
            await provider.nudge(handle(), REMINDER)

        assert caught.value.composer_dirty is False
        assert caught.value.session_name == handle().name
        assert composer.draft == ""
        assert composer.clears == 1
        assert provider._unsubmitted == {}

    async def test_without_clear_keys_the_text_is_kept_for_the_resubmit_path(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        provider = provider_for(composer)

        with pytest.raises(NotSubmitted) as caught:
            await provider.nudge(handle(), REMINDER)

        assert caught.value.composer_dirty is True
        assert composer.draft == REMINDER
        assert provider._unsubmitted[handle().name][0] == MARKER

    async def test_a_harness_with_clear_keys_that_do_nothing_reports_dirty(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99, clear_keys=("C-u",))
        composer.clear_keys = ("C-u",)
        provider = provider_for(composer)
        # A TUI that ignores the clear key too: mutate the handler so the
        # key is recorded but the draft survives.
        composer._reset_input = lambda: None

        with pytest.raises(NotSubmitted) as caught:
            await provider.nudge(handle(), REMINDER)

        assert caught.value.composer_dirty is True
        assert composer.draft == REMINDER


class TestResubmitInsteadOfDeferring:
    async def test_retry_presses_enter_on_our_own_text_without_retyping(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), REMINDER)
        assert composer.draft == REMINDER  # the stall state, reproduced

        composer.ignore_enters = 0
        composer.mutations.clear()

        await provider.nudge(handle(), REMINDER)

        assert composer.submitted == [REMINDER]
        assert typed_text(composer) == [], "the text was already there; do not type it twice"
        assert provider._unsubmitted == {}

    async def test_a_human_draft_still_defers(self, fast_polls):
        composer = FlakyComposer(draft="my own half-written note", cursor_x=25)
        provider = provider_for(composer)

        with pytest.raises(NudgeDeferred):
            await provider.nudge(handle(), REMINDER)

        assert composer.draft == "my own half-written note"
        assert composer.mutations == []

    async def test_copy_mode_is_never_treated_as_our_composer(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), REMINDER)

        composer.in_mode = 1
        composer.mutations.clear()
        with pytest.raises(NudgeDeferred):
            await provider.nudge(handle(), REMINDER)
        assert composer.mutations == []


class TestStuckComposerProbe:
    async def test_pending_submit_reports_then_clears_once_the_agent_submits(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), REMINDER)

        assert await provider.pending_submit(handle()) == MARKER

        composer._reset_input()  # the agent pressed Enter itself
        assert await provider.pending_submit(handle()) is None
        assert provider._unsubmitted == {}

    async def test_pending_submit_is_read_only(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), REMINDER)
        composer.mutations.clear()

        await provider.pending_submit(handle())

        assert composer.mutations == []

    async def test_resubmit_pending_presses_enter(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), REMINDER)

        composer.ignore_enters = 0
        assert await provider.resubmit_pending(handle()) is True
        assert composer.submitted == [REMINDER]
        assert await provider.pending_submit(handle()) is None

    async def test_resubmit_pending_is_a_no_op_when_nothing_is_stuck(self):
        composer = FlakyComposer()
        provider = provider_for(composer)
        assert await provider.resubmit_pending(handle()) is False
        assert composer.mutations == []


class TestMarkerOnInputLine:
    """Stricter than :func:`_submit_pending`: an unrecognised screen is
    "not ours", because a wrong yes resubmits a delivered message."""

    def test_text_on_the_input_line_is_ours(self):
        tail = "\n".join(["────────", f"❯ {REMINDER}", "────────"])
        assert _marker_on_input_line(tail, MARKER, "❯ ") is True

    def test_transcript_echo_above_an_empty_prompt_is_not_ours(self):
        tail = "\n".join([f"❯ {REMINDER}", "  (working)", "────────", "❯ ", "────────"])
        assert _marker_on_input_line(tail, MARKER, "❯ ") is False

    @pytest.mark.parametrize(
        ("tail", "prefix"),
        [
            (f"  {REMINDER}", "❯ "),  # no prompt line visible at all
            (f"❯ {REMINDER}", ""),  # session started before AQ_READY_PREFIX
        ],
        ids=["no-visible-prompt", "no-prefix-hint"],
    )
    def test_unrecognised_screens_are_not_ours(self, tail, prefix):
        assert _marker_on_input_line(tail, MARKER, prefix) is False
        # ...while the submit confirmation still fails safe on the same input.
        assert _submit_pending(tail, MARKER, prefix) is True
