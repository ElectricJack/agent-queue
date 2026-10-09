"""The strict usage-limit screen matcher the stall ladder uses (bold-rapids.8).

The fixtures are real wordings, not paraphrases:

* ``CLAUDE_PANE`` is a tmux capture (184x35, the daemon's pane geometry) of
  Claude Code 2.1.278 replaying a pool worker's transcript that ended on its
  limit — note the NBSP after the ``⎿`` gutter and the idle ``❯`` + NBSP
  prompt below it.  The same message appears 104 times in this box's
  transcripts.
* ``_codex_pane`` is the bottom of a real Codex pool-worker pane
  (slot-0, 2026-09-21, pasted by the operator into a vault session): the
  ``■ `` error cell, then the idle ``› Ask Codex to do anything`` composer.
* The Codex messages are verbatim ``usage_limit_exceeded`` errors from this
  box's ``~/.codex/sessions`` rollouts; codex 0.155.1 spells the apostrophe
  U+2019, older builds U+0027.
"""

from __future__ import annotations

import pytest

from src.sessions.exit_classifier import RATE_LIMIT_PATTERNS
from src.sessions.usage_limit_screen import (
    USAGE_LIMIT_TAIL_LINES,
    detect_usage_limit_screen,
    match_usage_limit_screen,
)

CLAUDE_LIMIT_LINE = "You've hit your session limit · resets 1:40am (America/Los_Angeles)"

CLAUDE_PANE = "\n".join(
    [
        "❯ You are running task wise-grove-39 in /home/jkern/dev/agent-queue2/.aq/worktrees/slot-8.",
        "  Run aq prime first and follow what it tells you.",
        "  When the work is done, close the task explicitly:",
        "    aq task close wise-grove-39 --outcome pass --work-outcome shipped",
        "    aq session drain-ack",
        "  Exiting without aq task close is treated as a failure, not a success.",
        f"  ⎿ \u00a0{CLAUDE_LIMIT_LINE}",
        "     /usage-credits to finish what you\u2019re working on.",
        "",
        "✻ Cooked for 0s · done Tuesday, Sep 1, 11:54 PM",
        *[""] * 12,
        " " * 165 + "◉ xhigh · /effort",
        "─" * 184,
        "❯\u00a0",
        "─" * 184,
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
    ]
)

CODEX_MESSAGES = [
    # codex 0.155.1 (U+2019), 2026-09-20 pool workers
    (
        "You\u2019ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at Sep 23rd, 2026 3:44 PM."
    ),
    (
        "You\u2019ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
        "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try "
        "again at 6:45 AM."
    ),
    # an older build (U+0027)
    (
        "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
        "to purchase more credits or try again at Sep 11th, 2026 7:09 AM."
    ),
]

# Observed by the operator on 2026-10-06. OpenCode repaints this countdown
# while the worker holds its claim, without making any provider progress.
OPENCODE_LIMIT_PANE = (
    "Free limit reached\n"
    " ■■■■■■⬝⬝ Free usage exceeded, subscribe to Go [retrying in 15h 2m]\n"
    "esc interrupt"
)


class TestOpenCode:
    def test_observed_free_usage_exhaustion_and_reset(self):
        screen = detect_usage_limit_screen(OPENCODE_LIMIT_PANE, opencode=True)
        assert screen is not None
        assert screen.usage_exhausted
        assert screen.retry_after == 15 * 3600 + 2 * 60
        assert screen.line == "Free usage exceeded, subscribe to Go [retrying in 15h 2m]"
        assert match_usage_limit_screen(OPENCODE_LIMIT_PANE) is None

    def test_footer_with_right_aligned_version_badge(self):
        # fresh-rapids-73: the live pane of four parked OpenCode 1.18.35 workers.
        # The badge after the interrupt hint defeated an end-of-line anchor.
        pane = (
            "┃  Build  Space Bunny  OpenCode Zen\n"
            "   ⬝⬝⬝⬝⬝⬝⬝⬝ Free usage exceeded, subscribe to Go [retrying in 4h 28m attempt #1]"
            "               esc interrupt    • OpenCode 1.18.35"
        )
        screen = detect_usage_limit_screen(pane, opencode=True)
        assert screen is not None and screen.usage_exhausted
        assert screen.retry_after == 4 * 3600 + 28 * 60
        assert screen.line == (
            "Free usage exceeded, subscribe to Go [retrying in 4h 28m attempt #1]"
        )

    def test_footer_without_a_duration_or_with_an_expand_hint(self):
        screen = detect_usage_limit_screen(
            "  ⬝⬝ Free usage exceeded [retrying attempt #3]  esc again to interrupt", opencode=True
        )
        assert screen is not None and screen.usage_exhausted and screen.retry_after is None
        long_retry = detect_usage_limit_screen(
            "  ⬝⬝ Service unavailable: upstream overloaded… (click to expand)"
            " [retrying in 2h attempt #4]  esc interrupt",
            opencode=True,
        )
        assert long_retry is not None and long_retry.retry_after == 7200
        assert detect_usage_limit_screen(
            "  ⬝⬝ Service unavailable [retrying attempt #4]  esc interrupt", opencode=True
        ) is None

    def test_retry_footer_with_animations_disabled(self):
        screen = detect_usage_limit_screen(
            " [⋯] Free limit reached [retrying in 15h 2m attempt #1] esc interrupt", opencode=True
        )
        assert screen is not None and screen.usage_exhausted and screen.retry_after == 54120

    @pytest.mark.parametrize(
        ("duration", "seconds"),
        [
            ("2h", 7200), ("1d 2h 3m 4s", 93784), ("90m", 5400), ("3s", 3),
            ("~1 day", 86400), ("~2 days", 172800), ("~1 week", 604800),
        ],
    )
    def test_free_limit_retry_durations(self, duration, seconds):
        screen = detect_usage_limit_screen(
            f"  Free limit reached [retrying in {duration} attempt #2]", opencode=True
        )
        assert screen is not None and screen.usage_exhausted
        assert screen.retry_after == seconds

    def test_long_server_retry_needs_corroboration(self):
        screen = detect_usage_limit_screen(
            "  ▄ Internal server error [retrying in 2h attempt #8] esc interrupt", opencode=True
        )
        assert screen is not None and screen.retry_after == 7200
        assert not screen.usage_exhausted

    @pytest.mark.parametrize("pane", [
        "  ▄ Internal server error [retrying in 30s attempt #2] esc interrupt",
        "Free usage exceeded, subscribe to Go",
        'print("Free usage exceeded, subscribe to Go [retrying in 15h 2m]")',
        "+Free usage exceeded, subscribe to Go [retrying in 15h 2m]",
        "src/file.py:42:Free usage exceeded, subscribe to Go [retrying in 15h 2m]",
        "› Free usage exceeded, subscribe to Go [retrying in 15h 2m]",
        "     Free usage exceeded, subscribe to Go [retrying in 15h 2m]",
        "  > Free usage exceeded, subscribe to Go [retrying in 15h 2m]",
        "  - Free usage exceeded, subscribe to Go [retrying in 15h 2m]",
        "Free usage exceeded [retrying in someday]",
    ])
    def test_ordinary_output_and_short_retries_are_left_alone(self, pane):
        assert detect_usage_limit_screen(pane, opencode=True) is None

    def test_old_retry_footer_is_history(self):
        pane = OPENCODE_LIMIT_PANE + "\n" + "\n".join(["Working"] * USAGE_LIMIT_TAIL_LINES)
        assert detect_usage_limit_screen(pane, opencode=True) is None


def _codex_pane(message: str) -> str:
    return "\n".join(
        [
            (
                "⚠ `--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run "
                "without review for this invocation."
            ),
            "• Automatically switched to Luna Reserve xhigh due to usage limits.",
            f"■ {message}",
            "› Ask Codex to do anything",
            "  Luna Reserve xhigh · ~/dev/agent-queue2/.aq/worktrees/slot-0",
        ]
    )


class TestRealWordings:
    def test_the_captured_claude_pane_matches(self):
        assert match_usage_limit_screen(CLAUDE_PANE) == CLAUDE_LIMIT_LINE

    @pytest.mark.parametrize("message", CODEX_MESSAGES)
    def test_codex_usage_limit_errors_match(self, message):
        assert match_usage_limit_screen(_codex_pane(message)) == message

    @pytest.mark.parametrize(
        "line",
        [
            # Claude Code 2.1.278 builds every blocking message from these
            # prefixes (its own "is this a limit message" list).
            "You've hit your limit · resets 3pm (America/Los_Angeles)",
            "You've hit your weekly limit · resets Sep 24, 9am (America/Los_Angeles)",
            "You've hit your Opus limit · resets 5am (UTC) · progress saved",
            "You've hit your usage limit",
            "You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
            "You've hit your org's monthly usage limit",
            "You've hit your team's shared budget · ask your admin to raise it",
            "You've reached your Fable limit.",
            "You're out of usage credits · resets 3pm",
            "You're out of extra usage",
            "Your org is out of usage · contact your admin",
            "Your organization is out of usage credits. Contact your admin to add more.",
            # Codex 0.155.1
            (
                "You\u2019ve hit your usage limit for gpt-5.6-sol. Switch to another model "
                "now, or try again at 3:44 PM."
            ),
            "You\u2019ve hit your usage limit.",
            "Your workspace is out of credits. Add credits to continue.",
            "You hit your spend cap set in your workspace. Increase your spend cap to continue.",
        ],
    )
    def test_every_blocking_wording_matches_in_either_gutter(self, line):
        assert match_usage_limit_screen(f"  ⎿ \u00a0{line}\n\n❯\u00a0") == line
        assert match_usage_limit_screen(f"■ {line}\n\n› ") == line


class TestStrictness:
    """False positives cost a WIP commit and a relaunch, so these must not match."""

    @pytest.mark.parametrize(
        "line",
        [
            # Warnings: the CLI is still working.
            "You've used 90% of your session limit · resets 3pm",
            "You're close to your weekly limit",
            "Approaching your usage limit",
            # Fast mode's cooldown notice: Claude keeps working at normal speed.
            "You've hit your fast limit · resets in 5m",
            # "Now using credits" notices: the CLI carries on.
            "You're now using usage credits",
            # The exit classifier's broad patterns, which ordinary output hits.
            "HTTP 429 Too Many Requests",
            "rate limit exceeded, retrying in 2s",
            "Claude usage limit reached",
        ],
    )
    def test_warnings_and_broad_rate_limit_text_do_not_match(self, line):
        assert match_usage_limit_screen(f"  ⎿ \u00a0{line}\n\n❯\u00a0") is None
        assert match_usage_limit_screen(f"■ {line}") is None

    def test_the_exit_classifier_patterns_would_have_matched_ordinary_output(self):
        """Why this is a separate set: ``429`` alone trips the broad one."""
        import re

        broad = re.compile("|".join(RATE_LIMIT_PATTERNS), re.IGNORECASE)
        assert broad.search("  ⎿  tests/test_pools.py::test_429_retry PASSED")
        assert match_usage_limit_screen("  ⎿  tests/test_pools.py::test_429_retry PASSED") is None

    @pytest.mark.parametrize(
        "line",
        [
            # A diff or file view of code that quotes the wording.
            f'  12 +    "{CLAUDE_LIMIT_LINE}",',
            f'+    "{CLAUDE_LIMIT_LINE}",',
            f"tests/test_x.py:12:    {CLAUDE_LIMIT_LINE}",
            # Prose that mentions it mid-sentence.
            f"  The worker printed {CLAUDE_LIMIT_LINE} and stopped.",
            # Text sitting in the composer is not the CLI's screen.
            f"❯ {CLAUDE_LIMIT_LINE}",
            f"› {CODEX_MESSAGES[0]}",
            # A markdown bullet or quote in agent output.
            f"  - {CLAUDE_LIMIT_LINE}",
            f"  > {CLAUDE_LIMIT_LINE}",
            # Claude paints a tool's output continuation lines indented, with
            # no gutter glyph: an agent grepping for the wording prints it so.
            f"     {CLAUDE_LIMIT_LINE}",
            "     You're out of usage credits. /model to switch models.",
            # ...and a saved pane shown as tool output keeps its glyph, but
            # five columns in.
            f"     \u25a0 {CODEX_MESSAGES[0]}",
            f"     \u23bf  {CLAUDE_LIMIT_LINE}",
        ],
    )
    def test_the_wording_quoted_inside_other_output_does_not_match(self, line):
        assert match_usage_limit_screen(f"{line}\n\n❯\u00a0") is None

    def test_a_limit_line_above_the_tail_is_history_not_the_screen(self):
        later = [f"  ⎿  line {i}" for i in range(USAGE_LIMIT_TAIL_LINES)]
        pane = "\n".join([f"  ⎿ \u00a0{CLAUDE_LIMIT_LINE}", *later])
        assert match_usage_limit_screen(pane) is None

    def test_blank_lines_do_not_push_the_limit_line_out_of_the_tail(self):
        """Claude pads the pane with a dozen blank rows below the message."""
        pane = "\n".join([f"  ⎿ \u00a0{CLAUDE_LIMIT_LINE}", *[""] * 40, "❯\u00a0"])
        assert match_usage_limit_screen(pane) == CLAUDE_LIMIT_LINE

    def test_codex_switching_models_on_its_limit_is_not_a_stop(self):
        """Codex falls back to a reserve model by itself and keeps working."""
        pane = "• Automatically switched to Luna Reserve xhigh due to usage limits.\n› "
        assert match_usage_limit_screen(pane) is None

    @pytest.mark.parametrize("pane", ["", "\n\n\n", "❯\u00a0"])
    def test_an_empty_or_idle_pane_does_not_match(self, pane):
        assert match_usage_limit_screen(pane) is None
