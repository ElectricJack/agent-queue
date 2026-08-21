"""Nudge submit-confirmation logic (pure helpers, no tmux server needed).

Live-test regression: Claude echoes the submitted prompt into the
transcript, so the old "marker anywhere in the capture" check reported
``NotSubmitted`` after every *successful* submit — the delivery engine
left the row pending and re-nudged the same envelope each idle window.
The confirm is now anchored to the last prompt-prefixed line (the input
line): unsubmitted text sits at/after it, a transcript echo sits above.
"""

from __future__ import annotations

from src.sessions.tmux import TmuxProvider, _submit_pending

NBSP = " "
MARKER = 'Reply with `aq reply msg-1 "…"`.'


class TestSubmitPending:
    def test_transcript_echo_above_an_empty_input_line_is_submitted(self):
        tail = "\n".join(
            [
                "❯ [msg-1 from user:cli] hello supervisor",
                f"  {MARKER}",
                "",
                "────────",
                "❯ ",
                "────────",
                "  ? for shortcuts",
            ]
        )
        assert _submit_pending(tail, MARKER, "❯ ") is False

    def test_text_still_in_the_input_line_is_pending(self):
        tail = "\n".join(
            [
                "  older transcript output",
                "────────",
                f"❯ [msg-1 from user:cli] hello supervisor {MARKER}",
                "────────",
                "  ? for shortcuts",
            ]
        )
        assert _submit_pending(tail, MARKER, "❯ ") is True

    def test_wrapped_input_continuation_lines_after_the_prompt_are_pending(self):
        tail = "\n".join(
            [
                "────────",
                "❯ [msg-1 from user:cli] a very long message that wraps",
                f"  {MARKER}",
                "────────",
            ]
        )
        assert _submit_pending(tail, MARKER, "❯ ") is True

    def test_no_prefix_hint_falls_back_to_whole_tail_scan(self):
        """Sessions started by an older daemon have no AQ_READY_PREFIX;
        the conservative fallback treats any visible marker as pending
        (costs a retry Enter, never silently drops a nudge)."""
        tail = f"❯ echoed {MARKER}\n❯ "
        assert _submit_pending(tail, MARKER, "") is True

    def test_marker_gone_from_the_tail_is_submitted(self):
        assert _submit_pending("❯ \n  ? for shortcuts", MARKER, "❯ ") is False

    def test_empty_marker_is_never_pending(self):
        assert _submit_pending("anything", "", "❯ ") is False

    def test_nbsp_prompt_prefix_matches_plain_space(self):
        """Claude paints ``❯`` + NBSP; harness files may spell either."""
        tail = "\n".join([f"❯{NBSP}echo {MARKER}", f"❯{NBSP}"])
        assert _submit_pending(tail, MARKER, f"❯{NBSP}") is False
        assert _submit_pending(tail, MARKER, "❯ ") is False

    def test_input_line_scrolled_out_of_window_stays_pending(self):
        """No visible prompt line + visible marker → assume pending."""
        tail = f"  {MARKER}\n  more wrapped paste"
        assert _submit_pending(tail, MARKER, "❯ ") is True


class TestCaptureTailTrims:
    async def test_capture_tail_returns_only_the_last_n_lines(self, tmp_path):
        """``capture-pane -S -N`` returns the whole screen plus N history
        lines — the tail must be trimmed client-side or the transcript
        echo is always "in the tail"."""

        class _Sessions:
            tmux_socket = "unused"

        class _Cfg:
            data_dir = str(tmp_path)
            sessions = _Sessions()

        provider = TmuxProvider(config=_Cfg())
        screen = "\n".join(f"line-{i}" for i in range(24)) + "\n"

        async def fake_tmux(*args, **kw):
            assert "capture-pane" in args
            return screen

        provider._tmux = fake_tmux
        tail = await provider._capture_tail("%0", lines=5)
        assert tail.splitlines() == [f"line-{i}" for i in range(19, 24)]
