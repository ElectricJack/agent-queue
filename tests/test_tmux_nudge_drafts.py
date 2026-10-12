"""Background reminders must not submit terminal drafts.

The fake below emulates the tmux boundary, including the distinction between
a dim TUI placeholder and literal input. No real agents or sockets are used.
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock

import pytest

if os.name != "posix":
    pytest.skip("tmux provider is POSIX-only", allow_module_level=True)

from src.sessions.provider import NotSubmitted, SessionHandle
from src.sessions.tmux import TmuxCommandError, TmuxProvider

REMINDER = "No progress for 8 min. Report status, finish the task, or run aq ask."
STALE_DRAFT = (
    "You are running task fresh-horizon in /project/.aq/worktrees/slot-0.\n"
    "Run aq prime first and follow what it tells you.\n"
    "When the work is done, close the task explicitly:\n"
    "  aq task close fresh-horizon --outcome pass --work-outcome shipped\n"
    "  aq session drain-ack\n"
    "Exiting without aq task close is treated as a failure, not a success."
)
CODEX_PLACEHOLDER = "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m"


class Composer:
    def __init__(
        self,
        *,
        draft="",
        prefix="› ",
        row=None,
        cursor_x=2,
        cursor_y=2,
        height=5,
        attached=0,
        visible=1,
        in_mode=0,
        below=None,
        above=None,
    ):
        self.draft = draft
        self.prefix = prefix
        self.cursor_x = cursor_x
        self.cursor_y = cursor_y
        self.height = height
        self.attached = attached
        self.visible = visible
        self.in_mode = in_mode
        self.row = row if row is not None else prefix + draft
        self.below = below if below is not None else ["", ""]
        self.above = above if above is not None else ["prior output"] * cursor_y
        self.submitted = []
        self.mutations = []
        self.buffer = b""
        self.typed = False
        self.fail_probe = False
        self.on_capture = None
        self.environment = {"AQ_READY_PREFIX": prefix, "AQ_SKIP_ESCAPE": "1"}

    async def tmux(self, *args, stdin=None, **kwargs):
        command = args[0]
        if command == "show-environment":
            key = args[-1]
            return f"{key}={self.environment.get(key, '')}\n"
        if command == "set-environment":
            if "-u" in args:
                self.environment.pop(args[-1], None)
            else:
                self.environment[args[-2]] = args[-1]
            return ""
        if command == "display-message":
            if self.fail_probe:
                raise TmuxCommandError(args, 1, "pane not available")
            values = {
                "cursor_x": self.cursor_x,
                "cursor_y": self.cursor_y,
                "pane_width": 100,
                "pane_height": self.height,
                "cursor_flag": self.visible,
                "pane_in_mode": self.in_mode,
                "session_attached": self.attached,
            }
            result = args[-1]
            for key, value in values.items():
                result = result.replace("#{" + key + "}", str(value))
            return result + "\n"
        if command == "capture-pane":
            row = self.prefix + self.draft if self.typed else self.row
            result = "\n".join(self.above + [row] + self.below) + "\n"
            if self.on_capture is not None:
                self.on_capture()
            return result
        self.mutations.append(args)
        if command == "send-keys" and "-l" in args:
            self.draft += args[-1]
            self.typed = True
        elif command == "send-keys" and args[-1] == "Enter":
            self.submitted.append(self.draft)
            self.draft = ""
            self.row = self.prefix
            self.typed = False
        elif command == "load-buffer":
            self.buffer = stdin
        elif command == "paste-buffer":
            self.draft += self.buffer.decode()
            self.typed = True
        return ""


def provider_for(composer):
    provider = TmuxProvider()
    provider.nudge_debounce_ms = 0
    provider._tmux = composer.tmux
    provider._fenced = AsyncMock(return_value=True)
    provider._process_names_hint = AsyncMock(return_value=("codex",))
    provider._find_agent_pane = AsyncMock(return_value="%1")
    provider._raw_activity = AsyncMock(return_value=100.0)
    return provider


def handle():
    return SessionHandle(provider="tmux", name="s-eager-current", instance_token="token")


class TestBackgroundNudgeDrafts:
    @pytest.mark.parametrize("prefix", ["› ", "❯ ", "> ", "┃"])
    @pytest.mark.parametrize("fields,reason", [
        ({"attached": 1}, "client attached"),
        ({"in_mode": 1}, "pane in copy mode"),
        ({"cursor_y": 8}, "cursor outside pane"),
        ({"prefix": ""}, "unknown prompt prefix"),
    ])
    async def test_refusal_names_the_guard_condition_without_typing(self, prefix, fields, reason):
        composer = Composer(**({"prefix": prefix} | fields))
        provider = provider_for(composer)
        with pytest.raises(NotSubmitted, match=reason):
            await provider.nudge(handle(), "Handle `aq message status msg-test --json`.")
        assert composer.mutations == []
        assert composer.submitted == []

    @pytest.mark.parametrize(
        "composer",
        [
            Composer(draft=STALE_DRAFT, cursor_x=75),
            Composer(draft="fresh-horizon", cursor_x=2),
            Composer(draft="\nrun stale task", row="› ", below=["  run stale task", ""]),
            Composer(draft="Ask Codex to do anything", cursor_x=2),
            Composer(row="still processing a tool", visible=0),
            Composer(prefix="", row=""),
            Composer(prefix="❯ ", row=CODEX_PLACEHOLDER),
            Composer(row=CODEX_PLACEHOLDER, attached=1),
            Composer(row=CODEX_PLACEHOLDER, in_mode=1),
            Composer(row="› ", cursor_x=0),
        ],
        ids=[
            "stale-other-task",
            "cursor-home-with-draft",
            "multiline-draft",
            "literal-placeholder-is-a-draft",
            "hidden-cursor",
            "unknown-prefix",
            "wrong-harness-prefix",
            "attached-portal",
            "copy-mode",
            "cursor-outside-input",
        ],
    )
    async def test_defers_without_touching_existing_input(self, composer):
        provider = provider_for(composer)
        original = composer.draft
        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), REMINDER)
        assert composer.draft == original
        assert composer.submitted == []
        assert composer.mutations == []
        assert provider._last_nudge_at == {}
        assert provider._poke == {}

    @pytest.mark.parametrize(
        "composer",
        [
            Composer(row=CODEX_PLACEHOLDER, below=["", "  90% context left"]),
            Composer(prefix="❯ ", row="❯\N{NO-BREAK SPACE}"),
            Composer(
                prefix="❯ ",
                row="❯\N{NO-BREAK SPACE}",
                below=["────────────────────", "  bypass permissions on"],
                above=["older output", "────────────────────"],
            ),
        ],
        ids=["codex-dim-placeholder", "claude-empty-prompt", "claude-bordered-prompt"],
    )
    async def test_known_empty_composer_gets_only_the_reminder(self, composer):
        provider = provider_for(composer)
        await provider.nudge(handle(), REMINDER)
        assert composer.submitted == [REMINDER]
        assert not any(args[-1] in {"Escape", "C-u"} for args in composer.mutations)

    async def test_failed_prompt_probe_defers_without_keys(self):
        composer = Composer(row=CODEX_PLACEHOLDER)
        composer.fail_probe = True
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.mutations == []

    async def test_attachment_appearing_during_capture_defers(self):
        composer = Composer(row=CODEX_PLACEHOLDER)
        composer.on_capture = lambda: setattr(composer, "attached", 1)
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.mutations == []


    async def test_recent_manual_input_defers_even_if_old_empty_frame_remains(self):
        composer = Composer(
            draft="manual instruction still being painted",
            row=CODEX_PLACEHOLDER,
        )
        provider = provider_for(composer)
        provider._last_input_at[handle().name] = time.monotonic()
        original = composer.draft

        with pytest.raises(NotSubmitted):
            await provider.nudge(handle(), REMINDER)

        assert composer.draft == original
        assert composer.submitted == []
        assert composer.mutations == []
        provider._fenced.assert_not_awaited()

    async def test_quiet_empty_composer_can_be_nudged_after_manual_input(self):
        composer = Composer(row=CODEX_PLACEHOLDER)
        provider = provider_for(composer)
        provider._last_input_at[handle().name] = time.monotonic() - 3.0

        await provider.nudge(handle(), REMINDER)

        assert composer.submitted == [REMINDER]

    @pytest.mark.parametrize("prefix", ["❯ ", "› "], ids=["claude", "codex"])
    @pytest.mark.parametrize("indent", ["", "  "], ids=["unindented", "continuation-indent"])
    @pytest.mark.parametrize(
        "below",
        [["────────────────────", "  status"], ["", ""]],
        ids=["bottom-border-only", "no-border"],
    )
    async def test_multiline_draft_line_that_looks_like_prompt_is_ambiguous(
        self, prefix, indent, below
    ):
        painted_prefix = prefix.replace(" ", "\N{NO-BREAK SPACE}")
        draft = "Existing multiline draft\n" + indent + painted_prefix
        composer = Composer(
            draft=draft,
            prefix=prefix,
            row=indent + painted_prefix,
            cursor_x=len(indent) + 2,
            above=["older output", painted_prefix + "Existing multiline draft"],
            below=below,
        )

        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)

        assert composer.draft == draft
        assert composer.submitted == []
        assert composer.mutations == []


class TestCodexFooterPadding:
    async def test_detached_empty_codex_with_bottom_padding_accepts_reminder(self):
        composer = Composer(
            row=CODEX_PLACEHOLDER,
            cursor_y=17,
            height=22,
            below=["", "  gpt-5.6-sol high · /project", "", ""],
        )
        await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.submitted == [REMINDER]

    # codex-cli 0.157 (observed live 2026-09-27): a model row, then one hint
    # row whose wording varies by build and state, with right-aligned notices.
    @pytest.mark.parametrize(
        "hint",
        [
            "  ? for shortcuts",
            "  ? for shortcuts" + " " * 40 + "⚠ 2 warnings · f2 to view",
            "  ← for agents · ? for shortcuts",
            " " * 60 + "⚠ 2 warnings · f2 to view",
        ],
        ids=["plain-hint", "hint-with-warnings", "agents-hint", "notice-only"],
    )
    @pytest.mark.parametrize("padding", [[], ["", ""]], ids=["no-padding", "padding"])
    async def test_two_row_codex_footer_accepts_reminder(self, hint, padding):
        model_row = "  GPT-6-Sol xhigh · ~/dev/agent-queue2/.aq/worktrees/slot-3 · Process tasks"
        below = ["", model_row, hint, *padding]
        composer = Composer(
            row=CODEX_PLACEHOLDER, cursor_y=17, height=18 + len(below), below=below
        )
        await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.submitted == [REMINDER]

    # The footer is bounded by shape, not wording: its one hint row changed
    # twice in a day (``? for shortcuts``, ``← for agents · ? for
    # shortcuts``) and each change left every idle Codex worker unwakeable.
    # The dim placeholder under the cursor is what proves the input empty.
    @pytest.mark.parametrize(
        "below",
        [
            ["", "  GPT-6-Sol xhigh · /project", "  ? for shortcuts", "  unsent continuation"],
            ["  unsent continuation", "  GPT-6-Sol xhigh · /project", "  ? for shortcuts"],
            ["", "  unsent continuation", "  GPT-6-Sol xhigh · /project", "  ? for shortcuts"],
            ["", "  tab to queue message    91% context left", "  ? for shortcuts"],
        ],
        ids=["two-rows-after-model-row", "no-separator", "row-above-footer", "busy-footer"],
    )
    async def test_two_row_footer_does_not_bypass_input_safety(self, below):
        composer = Composer(
            row=CODEX_PLACEHOLDER, cursor_y=17, height=18 + len(below), below=below
        )
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.submitted == []
        assert composer.mutations == []

    @pytest.mark.parametrize("unsafe", ["attached", "literal-placeholder", "text-after-footer"])
    async def test_padding_does_not_bypass_input_safety(self, unsafe):
        composer = Composer(
            row=CODEX_PLACEHOLDER,
            cursor_y=17,
            height=22,
            below=["", "  gpt-5.6-sol high · /project", "", ""],
        )
        if unsafe == "attached":
            composer.attached = 1
        elif unsafe == "literal-placeholder":
            composer.draft = "Ask Codex to do anything"
            composer.row = composer.prefix + composer.draft
        else:
            composer.below[-2:] = ["  unsent continuation", "  and more"]
        original = composer.draft
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.draft == original
        assert composer.submitted == []
        assert composer.mutations == []


CLAUDE_BORDER = "─" * 40
#: Claude Code 2.1.283 on a freshly started tmux server keeps the terminal
#: cursor hidden and paints its own: one inverse-video blank at the input.
CLAUDE_DRAWN_CURSOR_ROW = "❯\N{NO-BREAK SPACE}\x1b[7m \x1b[0m"


def claude_hidden_cursor(row=CLAUDE_DRAWN_CURSOR_ROW, *, bordered=True):
    return Composer(
        prefix="❯ ",
        row=row,
        visible=0,
        above=["older output", CLAUDE_BORDER if bordered else "older output"],
        below=[CLAUDE_BORDER if bordered else "", "  ⏵⏵ bypass permissions on"],
    )


class TestHiddenCursor:
    async def test_claudes_bordered_composer_with_its_drawn_cursor_is_empty(self):
        composer = claude_hidden_cursor()
        await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.submitted == [REMINDER]

    @pytest.mark.parametrize(
        "composer",
        [
            claude_hidden_cursor("❯\N{NO-BREAK SPACE}draft\x1b[7m \x1b[0m"),
            claude_hidden_cursor("❯\N{NO-BREAK SPACE}\x1b[7md\x1b[0mraft"),
            claude_hidden_cursor(bordered=False),
            Composer(row=CODEX_PLACEHOLDER, visible=0, below=["", "  GPT-6-Sol xhigh · /p"]),
            Composer(prefix="❯ ", row="❯ ", visible=0),
        ],
        ids=[
            "claude-draft",
            "claude-cursor-on-draft",
            "claude-without-borders",
            "codex-placeholder",
            "plain-prompt",
        ],
    )
    async def test_every_other_hidden_cursor_still_defers(self, composer):
        with pytest.raises(NotSubmitted):
            await provider_for(composer).nudge(handle(), REMINDER)
        assert composer.submitted == []
        assert composer.mutations == []
