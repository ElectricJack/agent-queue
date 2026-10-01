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

import base64
import hashlib
import json
import os
import textwrap
import time

import pytest

if os.name != "posix":
    pytest.skip("tmux provider is POSIX-only", allow_module_level=True)

from src.sessions import tmux as tmux_module
from src.sessions.provider import NotSubmitted, NudgeDeferred, SessionHandle
from src.sessions.tmux import _marker_for, _marker_on_input_line, _submit_pending
from tests.test_tmux_nudge_drafts import CODEX_PLACEHOLDER, Composer, handle, provider_for

REMINDER = (
    "No progress for 8 min on task stark-journey-63. Close or continue: "
    'run `aq task close stark-journey-63 --outcome pass --summary "..."`.'
)
MARKER = _marker_for(REMINDER)


async def test_collapsed_paste_keeps_evidence_without_submitting_or_clearing():
    composer = Composer(draft="[Pasted Content 4636 chars]")
    provider = provider_for(composer)
    record = tmux_module._PendingSubmit(
        instance_token=handle().instance_token, marker=MARKER, text=REMINDER,
    )
    await provider._remember_pending(handle(), record)
    composer.mutations.clear()
    assert await provider.pending_submit(handle()) is None
    assert await provider.resubmit_pending(handle()) is False
    assert provider._unsubmitted[handle().name] == record
    assert composer.mutations == []


async def test_busy_footer_preserves_notification_until_idle_composer_is_observable():
    text = "Read `aq message status msg-example --json`."
    composer = Composer(draft=text, below=["", "  tab to queue message    91% context left"])
    provider = provider_for(composer)
    record = tmux_module._PendingSubmit(
        instance_token=handle().instance_token, marker=_marker_for(text), text=text,
    )
    await provider._remember_pending(handle(), record)
    composer.mutations.clear()
    assert await provider.resubmit_pending(handle()) is False
    assert provider._unsubmitted[handle().name] == record
    assert composer.mutations == []
    composer.below = ["", "  91% context left"]
    assert await provider.resubmit_pending(handle()) is True
    assert composer.submitted == [text]


@pytest.mark.parametrize(
    "below",
    [
        ["", "  GPT-6-Sol xhigh · ~/dev/agent-queue2/.aq/worktrees/slot-3", ""],
        ["", "  GPT-6-Sol xhigh · ~/dev/agent-queue2/.aq/worktrees/slot-3", "  ? for shortcuts"],
        # Live capture (-J): the hint row is painted over with spaces once
        # the composer holds text, and the model row keeps trailing cells.
        ["", "  GPT-6-Sol xhigh · ~/dev/agent-queue2/.aq/worktrees/slot-3  ", " " * 17],
    ],
    ids=["model-row", "model-and-hint-rows", "erased-hint-row"],
)
async def test_codex_0157_footer_is_observable_for_exact_resubmit(below):
    # codex-cli 0.157 capitalises its model row ("GPT-6-Sol"); a lowercase-only
    # footer match read every typed notification as "unobservable".
    text = "Handle `aq message status msg-example --json`."
    composer = Composer(draft=text, below=below)
    provider = provider_for(composer)
    record = tmux_module._PendingSubmit(
        instance_token=handle().instance_token, marker=_marker_for(text), text=text,
    )
    await provider._remember_pending(handle(), record)
    assert await provider.resubmit_pending(handle()) is True
    assert composer.submitted == [text]


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
            self.environment["AQ_CLEAR_KEYS"] = ",".join(self.clear_keys)
            return f"{key}={self.environment.get(key, '')}\n"
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
        assert provider._unsubmitted[handle().name].marker == MARKER

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
    async def test_pending_submit_survives_a_provider_restart(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)

        restarted = provider_for(composer)

        assert await restarted.pending_submit(handle()) == MARKER
        composer.ignore_enters = 0
        assert await restarted.resubmit_pending(handle()) is True
        assert composer.submitted == [REMINDER]
        assert "AQ_PENDING_SUBMIT" not in composer.environment

    async def test_edited_aq_text_is_never_resubmitted_after_restart(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)
        # Keep the same trailing marker but change the actual AQ injection.
        composer.draft = REMINDER.replace("Close", "Cloxe")
        composer.typed = True
        restarted = provider_for(composer)

        assert await restarted.pending_submit(handle()) is None
        assert await restarted.resubmit_pending(handle()) is False
        assert composer.submitted == []
        assert composer.enters == 4

    async def test_unknown_pane_preserves_durable_evidence_for_a_later_probe(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)
        restarted = provider_for(composer)
        # A provider restart normally recreates pane discovery. Simulate a
        # temporarily absent pane without deleting its tmux environment.
        from unittest.mock import AsyncMock

        restarted._find_agent_pane = AsyncMock(return_value=None)
        assert await restarted.pending_submit(handle()) is None
        assert "AQ_PENDING_SUBMIT" in composer.environment

        restarted._find_agent_pane = AsyncMock(return_value="%1")
        assert await restarted.pending_submit(handle()) == MARKER

    async def test_reused_name_cannot_claim_an_old_pending_record(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)
        restarted = provider_for(composer)
        successor = SessionHandle(provider="tmux", name=handle().name, instance_token="new")

        assert await restarted.pending_submit(successor) is None
        assert await restarted.resubmit_pending(successor) is False
        assert composer.submitted == []

    async def test_duplicate_recovery_only_submits_once(self, fast_polls):
        composer = FlakyComposer(ignore_enters=99)
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), REMINDER)
        composer.ignore_enters = 0

        assert await provider.resubmit_pending(handle()) is True
        assert await provider.resubmit_pending(handle()) is False
        assert composer.submitted == [REMINDER]

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


@pytest.mark.parametrize(
    "edit",
    [
        lambda text: "human prefix " + text,
        lambda text: text + " human suffix",
        lambda text: text + "\nhuman continuation",
        lambda text: "human continuation\n" + text,
    ],
    ids=["prepended", "appended", "newline-appended", "newline-prepended"],
)
async def test_recovery_refuses_human_text_surrounding_injection(fast_polls, edit):
    """A preserved AQ marker never authorizes Enter for an edited draft."""
    composer = FlakyComposer(ignore_enters=99)
    with pytest.raises(NotSubmitted):
        await provider_for(composer).nudge(handle(), REMINDER)
    composer.draft = edit(REMINDER)
    composer.typed = True
    composer.mutations.clear()
    restarted = provider_for(composer)

    assert await restarted.pending_submit(handle()) is None
    assert await restarted.resubmit_pending(handle()) is False
    assert composer.submitted == []
    assert not any(command[0] == "send-keys" for command in composer.mutations)


# ---------------------------------------------------------------------------
# Input the harness wraps itself (steady-cascade reopen, 2026-09-27)
# ---------------------------------------------------------------------------

#: The stall reminder as the reconciler words it; ~430 characters, so every
#: pane narrower than that shows it on several rows.
LONG_REMINDER = (
    "No progress for 12 min on task wise-ember.17. Close or continue: if the work is "
    'done run `aq task close wise-ember.17 --outcome pass|fail --summary "..."` then '
    "`aq session drain-ack`; if it is not done, keep working and run `aq task heartbeat "
    "wise-ember.17`; if you are blocked, say so with `aq message send --to user:dashboard "
    '--project "$AQ_PROJECT_ID" --body "Blocked: <question>"`.'
)
MESSAGE = "Handle `aq message status msg-9208c3d7479a4c188b8fea330ead76f0 --json`."

#: An idle codex-cli 0.157.1 pane (captured 2026-09-27): model row, then a
#: hint row whose wording differs from the ``? for shortcuts`` of other panes.
CODEX_IDLE_FOOTER = ["", "  GPT-6-Sol xhigh · /tmp/codexprobe/wd", "  ← for agents · ? for shortcuts"]
#: The footer once the composer holds text.  Codex paints the hint over with
#: spaces; the live wise-ember.17 pane kept its right-aligned notice.
CODEX_TYPED_FOOTERS = {
    "erased-hint": ["", "  GPT-6-Sol xhigh · /tmp/codexprobe/wd", " " * 32],
    "kept-notice": [
        "",
        "  GPT-6-Sol xhigh · ~/dev/agent-queue2/.aq/worktrees/slot-3 · Process agent-queue tasks",
        " " * 100 + "⚠ 2 warnings · f2 to view",
    ],
}
CLAUDE_BORDER = "─" * 80
CLAUDE_FOOTER = [CLAUDE_BORDER, "  ⏵⏵ bypass permissions on (shift+tab to cycle)"]


class WrappingComposer(Composer):
    """Paints input the way Codex and Claude do: word-wrapped onto rows of its
    own with a two-space continuation indent -- explicit rows, which
    ``capture-pane -J`` cannot join -- then the harness footer."""

    def __init__(self, *, width=80, idle_footer, typed_footer, above=None, **kw):
        above = above if above is not None else ["prior output"] * 3
        super().__init__(
            above=above,
            cursor_y=len(above),
            height=len(above) + 1 + len(idle_footer),
            below=idle_footer,
            **kw,
        )
        self.width = width
        self.typed_footer = typed_footer
        self.idle_row = self.row

    async def tmux(self, *args, stdin=None, **kwargs):
        if args[0] == "capture-pane" and self.draft:
            rows = textwrap.wrap(self.prefix + self.draft, self.width, subsequent_indent="  ")
            return "\n".join(self.above + rows + self.typed_footer) + "\n"
        result = await super().tmux(*args, stdin=stdin, **kwargs)
        if not self.draft:
            self.row = self.idle_row  # an emptied composer repaints its idle row
        return result


def codex_composer(width=80, typed="erased-hint"):
    return WrappingComposer(
        row=CODEX_PLACEHOLDER,
        width=width,
        idle_footer=CODEX_IDLE_FOOTER,
        typed_footer=CODEX_TYPED_FOOTERS[typed],
    )


def claude_composer(width=80):
    return WrappingComposer(
        prefix="❯ ",
        row="❯\N{NO-BREAK SPACE}",
        width=width,
        above=["older output", CLAUDE_BORDER],
        idle_footer=CLAUDE_FOOTER,
        typed_footer=CLAUDE_FOOTER,
    )


def stuck_record(text):
    return tmux_module._PendingSubmit(
        instance_token=handle().instance_token, marker=_marker_for(text), text=text,
    )


class TestHarnessWrappedInput:
    """Live failure: every idle Codex worker's stall reminder sat typed but
    never submitted -- row-by-row matching could not see text the composer
    had wrapped -- and every later message nudge deferred behind it."""

    @pytest.mark.parametrize("width", [60, 80, 190])
    @pytest.mark.parametrize("typed", sorted(CODEX_TYPED_FOOTERS))
    async def test_a_wrapped_reminder_is_typed_and_submitted_in_codex(
        self, fast_polls, width, typed
    ):
        composer = codex_composer(width, typed)
        await provider_for(composer).nudge(handle(), LONG_REMINDER)
        assert composer.submitted == [LONG_REMINDER]

    @pytest.mark.parametrize("width", [60, 80])
    async def test_a_wrapped_reminder_is_typed_and_submitted_in_claude(self, fast_polls, width):
        composer = claude_composer(width)
        await provider_for(composer).nudge(handle(), LONG_REMINDER)
        assert composer.submitted == [LONG_REMINDER]

    @pytest.mark.parametrize("make", [codex_composer, claude_composer], ids=["codex", "claude"])
    async def test_a_stale_injection_is_submitted_then_the_new_wake_typed(self, fast_polls, make):
        """The reminder names its idle minutes, so no later nudge ever matched
        the text left behind; deferring on "a different AQ injection" was a
        permanent stall.  AQ's own text is submitted, then the new wake."""
        composer = make()
        composer.draft = LONG_REMINDER  # typed by a daemon that never saw Enter land
        await provider_for(composer)._remember_pending(handle(), stuck_record(LONG_REMINDER))
        restarted = provider_for(composer)  # only the durable tmux record survives

        assert await restarted.pending_submit(handle()) == _marker_for(LONG_REMINDER)
        await restarted.nudge(handle(), MESSAGE)
        assert composer.submitted == [LONG_REMINDER, MESSAGE]
        assert await restarted.pending_submit(handle()) is None

    async def test_an_edited_stale_injection_still_defers_the_new_wake(self, fast_polls):
        composer = codex_composer()
        composer.draft = LONG_REMINDER + " and a human note"
        await provider_for(composer)._remember_pending(handle(), stuck_record(LONG_REMINDER))
        composer.mutations.clear()
        with pytest.raises(NudgeDeferred):
            await provider_for(composer).nudge(handle(), MESSAGE)
        assert composer.submitted == []
        assert composer.mutations == []

    async def test_an_unreadable_injection_is_reported_but_never_submitted(self, fast_polls):
        composer = WrappingComposer(
            row=CODEX_PLACEHOLDER,
            idle_footer=CODEX_IDLE_FOOTER,
            typed_footer=["", "  tab to queue message    91% context left"],
        )
        composer.draft = MESSAGE
        provider = provider_for(composer)
        await provider._remember_pending(handle(), stuck_record(MESSAGE))
        composer.mutations.clear()

        assert await provider.pending_submit(handle()) is None
        assert await provider.pending_submit_detail(handle()) == {
            "marker": _marker_for(MESSAGE),
            "observable": False,
            "clearable": False,
        }
        assert await provider.resubmit_pending(handle()) is False
        assert composer.submitted == []
        assert composer.mutations == []


# ---------------------------------------------------------------------------
# AQ text the composer cannot show verbatim (clear-lantern-82, 2026-10-01)
# ---------------------------------------------------------------------------


class ClaudeComposer:
    """Claude Code 2.1.286's composer as probed in an 80x24 tmux pane.

    Measured on a private socket, never submitting:

    - a typed burst over 800 characters becomes ``[Pasted text #N +M lines]``,
      where ``N`` counts this process's pastes and ``M`` is the newline count;
    - input taller than ``window`` rows shows only its last rows, with ``❯``
      drawn on the first visible one;
    - ``C-u`` kills the last visual row, or the newline under an empty last
      row, and does nothing on an empty composer;
    - Enter submits, expanding a placeholder to the text behind it.

    Rows are hard-wrapped where Claude breaks at words; every provider check
    ignores whitespace, so the difference cannot matter here.
    """

    BORDER = "─" * 80

    def __init__(self, *, width=80, height=24, window=7, clear_keys=("C-u",)):
        self.width = width
        self.height = height
        self.window = window
        self.chunks: list[tuple[str, str, str]] = []  # (kind, shown, content)
        self.pastes = 0
        self.attached = 0
        self.in_mode = 0
        self.submitted: list[str] = []
        self.mutations: list[tuple] = []
        self.buffer = ""
        self.on_key = None
        self.environment = {
            "AQ_READY_PREFIX": "❯ ",
            "AQ_SKIP_ESCAPE": "1",
            "AQ_CLEAR_KEYS": ",".join(clear_keys),
        }

    # -- the composer --------------------------------------------------------

    def type(self, text: str) -> None:
        if len(text) > 800:
            self.pastes += 1
            lines = text.count("\n")
            token = f"[Pasted text #{self.pastes}" + (f" +{lines} lines" if lines else "") + "]"
            self.chunks.append(("paste", token, text))
        elif self.chunks and self.chunks[-1][0] == "text":
            joined = self.chunks[-1][1] + text
            self.chunks[-1] = ("text", joined, joined)
        else:
            self.chunks.append(("text", text, text))

    @property
    def draft(self) -> str:
        return "".join(shown for _, shown, _ in self.chunks)

    def rows(self) -> list[str]:
        span = self.width - 2
        rows: list[str] = []
        for line in self.draft.split("\n"):
            rows.extend([line[i : i + span] for i in range(0, len(line), span)] or [""])
        return rows

    def kill_row(self) -> None:
        if not self.chunks:
            return
        kind, text, _ = self.chunks.pop()
        if kind == "paste":
            return
        if text.endswith("\n"):
            text = text[:-1]
        else:
            span = self.width - 2
            text = text[: -(len(text.rsplit("\n", 1)[-1]) % span or span)]
        if text:
            self.chunks.append(("text", text, text))

    def screen(self) -> list[str]:
        rows = self.rows()[-self.window :]
        body = ["❯\N{NO-BREAK SPACE}" + rows[0]] + ["  " + row for row in rows[1:]]
        lines = ["older output", self.BORDER, *body, self.BORDER, "  ⏵⏵ bypass permissions on"]
        return [""] * (self.height - len(lines)) + lines

    # -- the tmux boundary ---------------------------------------------------

    async def tmux(self, *args, stdin=None, **kwargs):
        command = args[0]
        if command == "show-environment":
            return f"{args[-1]}={self.environment.get(args[-1], '')}\n"
        if command == "set-environment":
            if "-u" in args:
                self.environment.pop(args[-1], None)
            else:
                self.environment[args[-2]] = args[-1]
            return ""
        if command == "display-message":
            rows = self.rows()[-self.window :]
            values = {
                "cursor_x": 2 + (len(rows[-1]) if self.chunks else 0),
                "cursor_y": self.height - 3,
                "pane_width": self.width,
                "pane_height": self.height,
                "window_height": self.height,
                "cursor_flag": 1,
                "pane_in_mode": self.in_mode,
                "session_attached": self.attached,
            }
            result = args[-1]
            for key, value in values.items():
                result = result.replace("#{" + key + "}", str(value))
            return result + "\n"
        if command == "capture-pane":
            return "\n".join(self.screen()) + "\n"
        self.mutations.append(args)
        if command == "send-keys" and "-l" in args:
            self.type(args[-1])
        elif command == "send-keys" and args[-1] == "Enter":
            if self.chunks:
                self.submitted.append("".join(content for _, _, content in self.chunks))
            self.chunks = []
        elif command == "send-keys" and args[-1] == "C-u":
            self.kill_row()
        elif command == "load-buffer":
            self.buffer = stdin.decode()
        elif command == "paste-buffer":
            self.type(self.buffer)
        if command == "send-keys" and self.on_key is not None:
            self.on_key(args[-1])
        return ""

    def sent_keys(self) -> list[str]:
        return [args[-1] for args in self.mutations if args[0] == "send-keys" and "-l" not in args]


def comment_notification(body: str) -> str:
    """The task-comment wake-up exactly as the daemon used to type it."""
    from src.commands.task_comment_commands import TaskCommentCommandsMixin

    task = type("T", (), {"id": "repair-repair-batch-integration-batch-a15aca37ba71d0ed81b96d73a8f624b7-1"})
    return TaskCommentCommandsMixin._render_comment_notification(
        task,
        {
            "id": "comment-9e376b24deac4d7282cf41a4df640c99",
            "author_kind": "supervisor",
            "author_id": "0b4a47c1-377f-4d41-bfa7-0cd0d7c10674",
            "created_at": 1790840212.3479939,
            "body": body,
        },
    )


HOLD = "Handled wait52a18fd2 expiry after fresh state and CI inspection. Preserve the claim. "
#: 928 characters on 7 lines: collapsed to ``[Pasted text #1 +6 lines]`` (the
#: 07:37Z incident on p-standard-high-claude--agent-queue--37a7f8cc).
COLLAPSED_COMMENT = comment_notification((HOLD * 9)[:691].strip())
#: Under 800 characters but ~14 rows at 80 columns: Claude shows its last 7
#: (the 07:11Z incident, msg-5ba6aa4e).
WINDOWED_COMMENT = comment_notification((HOLD * 6)[:480].strip())
DEPLOY_READY = "Handle `aq message status msg-f3d2ec0095a8492e99ac1ad8dbb2abb3 --json`."


@pytest.fixture
def fast_composer(fast_polls, monkeypatch):
    monkeypatch.setattr(tmux_module, "_LANDED_POLL_SECONDS", 0.0)
    monkeypatch.setattr(tmux_module, "_CLEAR_SETTLE_SECONDS", 0.0)


def test_the_fixtures_reproduce_both_live_shapes():
    collapsed = ClaudeComposer()
    collapsed.type(COLLAPSED_COMMENT)
    assert collapsed.draft == "[Pasted text #1 +6 lines]"
    assert len(COLLAPSED_COMMENT) > 800 and COLLAPSED_COMMENT.count("\n") == 6

    windowed = ClaudeComposer()
    windowed.type(WINDOWED_COMMENT)
    shown = "\n".join(windowed.screen())
    assert len(WINDOWED_COMMENT) <= 800 and len(windowed.rows()) > windowed.window
    assert "[Task comment]" not in shown and _marker_for(WINDOWED_COMMENT) in shown.replace("\n  ", "")


class TestTextTheComposerCannotShowVerbatim:
    """The composer is the interlock for every later nudge, so AQ text left
    there unconfirmed blocked the supervisor's deploy-ready notice for twenty
    minutes.  AQ's own collapsed or windowed text is cleared -- never
    submitted, never acknowledged -- and anything AQ cannot attribute is left
    exactly as it is."""

    @pytest.mark.parametrize(
        "text", [COLLAPSED_COMMENT, WINDOWED_COMMENT], ids=["collapsed", "windowed"]
    )
    async def test_fresh_injection_is_cleared_and_never_submitted(self, fast_composer, text):
        composer = ClaudeComposer()
        provider = provider_for(composer)

        with pytest.raises(NotSubmitted) as caught:
            await provider.nudge(handle(), text)

        assert caught.value.composer_dirty is False
        assert composer.chunks == [] and composer.submitted == []
        assert set(composer.sent_keys()) == {"C-u"}, "only the clear key; never Enter, Escape or C-c"
        assert "AQ_PENDING_SUBMIT" not in composer.environment
        assert provider._unsubmitted == {}

    async def test_the_next_wake_is_delivered_into_the_emptied_composer(self, fast_composer):
        composer = ClaudeComposer()
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), WINDOWED_COMMENT)

        await provider.nudge(handle(), DEPLOY_READY)

        assert composer.submitted == [DEPLOY_READY]

    @pytest.mark.parametrize(
        "text", [COLLAPSED_COMMENT, WINDOWED_COMMENT], ids=["collapsed", "windowed"]
    )
    async def test_stranded_injection_is_recovered_after_a_daemon_restart(
        self, fast_composer, text
    ):
        """No clear key at first (this box's vault harness): the text stays
        and is reported dirty, and the durable record carries what AQ saw.
        Once the key exists, a restarted daemon's next wake clears it and is
        delivered -- the stranded notification is never submitted."""
        composer = ClaudeComposer(clear_keys=())
        with pytest.raises(NotSubmitted) as caught:
            await provider_for(composer).nudge(handle(), text)
        assert caught.value.composer_dirty is True
        stranded = composer.draft
        record = tmux_module._PendingSubmit.decode(composer.environment["AQ_PENDING_SUBMIT"])
        assert record.text == text
        if text is COLLAPSED_COMMENT:
            assert record.placeholder == "[Pasted text #1 +6 lines]" == stranded

        composer.environment["AQ_CLEAR_KEYS"] = "C-u"
        restarted = provider_for(composer)
        assert await restarted.pending_submit_detail(handle()) == {
            "marker": _marker_for(text),
            "observable": False,
            "clearable": True,
        }
        keys = composer.sent_keys()
        restarted._last_input_at[handle().name] = time.monotonic()  # a human is typing
        with pytest.raises(NudgeDeferred):
            await restarted.nudge(handle(), DEPLOY_READY)
        assert await restarted.clear_pending(handle()) is False
        assert composer.draft == stranded and composer.sent_keys() == keys
        restarted._last_input_at.clear()

        await restarted.nudge(handle(), DEPLOY_READY)

        assert composer.submitted == [DEPLOY_READY]
        assert "AQ_PENDING_SUBMIT" not in composer.environment
        assert await restarted.pending_submit_detail(handle()) is None

    async def test_a_record_from_before_placeholders_were_recorded_is_attributed(
        self, fast_composer
    ):
        """The live 07:37Z record, as the old daemon encoded it: no placeholder
        field.  Its 928 characters must have collapsed, and its six newlines
        match ``+6 lines``."""
        composer = ClaudeComposer()
        composer.type(COLLAPSED_COMMENT)
        legacy = {
            "v": 1,
            "instance_token": handle().instance_token,
            "marker": _marker_for(COLLAPSED_COMMENT),
            "text": COLLAPSED_COMMENT,
            "text_sha256": hashlib.sha256(COLLAPSED_COMMENT.encode()).hexdigest(),
        }
        composer.environment["AQ_PENDING_SUBMIT"] = base64.urlsafe_b64encode(
            json.dumps(legacy).encode()
        ).decode()

        await provider_for(composer).nudge(handle(), DEPLOY_READY)

        assert composer.submitted == [DEPLOY_READY]
        assert composer.sent_keys() == ["C-u", "Enter"]

    async def test_the_doctor_fix_clears_without_pressing_enter(self, fast_composer):
        composer = ClaudeComposer(clear_keys=())
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), COLLAPSED_COMMENT)
        composer.environment["AQ_CLEAR_KEYS"] = "C-u"

        assert await provider.resubmit_pending(handle()) is False  # never Enter on it
        assert await provider.clear_pending(handle()) is True
        assert await provider.clear_pending(handle()) is False  # nothing left to clear

        assert composer.chunks == [] and composer.submitted == []
        assert "Enter" not in composer.sent_keys()


def _stranded(composer, text, *, placeholder=""):
    """AQ's durable record for *text*, as a previous daemon left it."""
    record = tmux_module._PendingSubmit(
        instance_token=handle().instance_token,
        marker=_marker_for(text),
        text=text,
        placeholder=placeholder,
    )
    composer.environment["AQ_PENDING_SUBMIT"] = record.encode()


def _ours_then(edit):
    def arrange(composer):
        composer.type(COLLAPSED_COMMENT)
        _stranded(composer, COLLAPSED_COMMENT, placeholder=composer.draft)
        edit(composer)

    return arrange


def _human_paste_after_ours(composer):
    """The operator cleared AQ's paste and pasted a draft of the same shape."""
    _ours_then(lambda c: (c.kill_row(), c.type(COLLAPSED_COMMENT.upper())))(composer)


def _legacy_line_mismatch(composer):
    composer.type(COLLAPSED_COMMENT + "\none more line")
    _stranded(composer, COLLAPSED_COMMENT)


def _legacy_short_text(composer):
    """Seven short AQ lines could never have collapsed: the paste is a human's."""
    composer.type(COLLAPSED_COMMENT.upper())
    _stranded(composer, "\n".join(["a short AQ line"] * 7))


def _windowed_with_human_suffix(composer):
    composer.type(WINDOWED_COMMENT + " -- and please also rerun the e2e smoke")
    _stranded(composer, WINDOWED_COMMENT)


def _placeholder_then_human_text(composer):
    _ours_then(lambda c: c.type(" and my own note"))(composer)


def _attached(composer):
    _ours_then(lambda c: setattr(c, "attached", 1))(composer)


def _copy_mode(composer):
    _ours_then(lambda c: setattr(c, "in_mode", 1))(composer)


@pytest.mark.parametrize(
    "arrange",
    [
        _human_paste_after_ours,
        _legacy_line_mismatch,
        _legacy_short_text,
        _windowed_with_human_suffix,
        _placeholder_then_human_text,
        _attached,
        _copy_mode,
    ],
    ids=[
        "human-paste-different-id",
        "legacy-line-count-mismatch",
        "legacy-text-too-short-to-collapse",
        "windowed-with-human-suffix",
        "placeholder-plus-human-text",
        "attached-client",
        "copy-mode",
    ],
)
async def test_input_aq_cannot_attribute_is_never_touched(fast_composer, arrange):
    composer = ClaudeComposer()
    arrange(composer)
    before = composer.draft
    restarted = provider_for(composer)

    with pytest.raises(NudgeDeferred):
        await restarted.nudge(handle(), DEPLOY_READY)
    assert await restarted.clear_pending(handle()) is False

    assert composer.draft == before
    assert composer.sent_keys() == [] and composer.submitted == []
    assert "AQ_PENDING_SUBMIT" in composer.environment, "keep the evidence for a later pass"


@pytest.mark.parametrize(
    ("attaches", "on_press"),
    [(True, 1), (False, 7)],
    ids=["client-attaches-mid-batch", "text-arrives-between-reads"],
)
async def test_a_clear_stops_when_someone_else_starts_typing(fast_composer, attaches, on_press):
    """A client attaching stops the clear before its next key, and every
    screen the clear reads must still be a piece of AQ's text.  (Dashboard
    input shares the nudge lock, so it can only land between reads.)"""
    composer = ClaudeComposer(clear_keys=())
    with pytest.raises(NotSubmitted):
        await provider_for(composer).nudge(handle(), WINDOWED_COMMENT)
    composer.environment["AQ_CLEAR_KEYS"] = "C-u"
    presses = []

    def human_types(key):
        presses.append(key)
        if len(presses) == on_press:
            composer.attached = int(attaches)
            composer.type(" wait, keep this")

    composer.on_key = human_types

    with pytest.raises(NudgeDeferred):
        await provider_for(composer).nudge(handle(), DEPLOY_READY)

    assert composer.draft.endswith(" wait, keep this")
    assert composer.sent_keys() == ["C-u"] * on_press
    assert composer.submitted == []
    assert "AQ_PENDING_SUBMIT" in composer.environment
