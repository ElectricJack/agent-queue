"""Nudging an OpenCode worker: its box composer, and plugin output painted over it.

Live failure (2026-09-26/27, agile-willow, agile-torrent.12, prime-glacier.8):
an OpenCode worker ended its turn after an auto-compaction and sat idle for
20+ minutes until the supervisor typed a prompt.  The stall ladder's "close
or continue" nudge never arrived, for two reasons this module pins:

* the composer guard knew only Claude's and Codex's prompt lines, so an
  idle OpenCode box never read as empty, and the submit checks anchored on
  the last bar-prefixed row, which in OpenCode is the agent row *below* the
  input;
* the fast-jev plugin, running inside the OpenCode process, logged each
  compaction with ``process.stderr.write``.  That paints over the composer
  and OpenCode's renderer never redraws it, so even a recognised box showed
  "a draft" until something forced a full frame.

Layouts are the ones measured on OpenCode 1.18.32 in a scratch tmux socket;
no real tmux server or model is used here.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

if os.name != "posix":
    pytest.skip("tmux provider is POSIX-only", allow_module_level=True)

from src.sessions import tmux as tmux_module
from src.sessions.provider import NotSubmitted, SessionHandle
from src.sessions.tmux import (
    TmuxProvider,
    _composer_is_empty,
    _composer_preview,
    _marker_for,
    _marker_on_input_line,
    _submit_pending,
)

BAR = "┃"
AGENT = "Build · Qwen3.8 27B (local) Ollama (local)"
FAST_JEV = (
    "[fast-jev:opencode 2026-09-27T10:14:22.594Z] session "
    "ses_f1dc6608cffeLei5eKxfQDVyIC: 64 msgs, calls=85 kept=0 dropped=56"
)
REMINDER = (
    "No progress for 21 min on task prime-glacier.8. Close or continue: if the work is "
    'done run `aq task close prime-glacier.8 --outcome pass|fail --summary "..."` then '
    "`aq session drain-ack`; if it is not done, keep working and run `aq task heartbeat "
    "prime-glacier.8`; if you are blocked, say so with `aq message send --to user:dashboard "
    '--project "$AQ_PROJECT_ID" --body "Blocked: <question>"`.'
)
#: The transcript above the composer uses the same bar at the same indent.
TRANSCRIPT = [
    "  ┃",
    "  ┃  $ echo probe-shell-mode",
    "  ┃",
    "  ┃  probe-shell-mode",
    "  ┃",
    "",
    "     ▣  Build · Qwen3.8 27B (local)",
    "",
]


class OpenCodePane:
    """The tmux boundary around one OpenCode TUI pane.

    ``render_captures`` is how many screen reads typed text takes to appear
    in full (OpenCode renders keystroke by keystroke); ``swallow`` drops it.
    A one-row window shrink followed by the restore is a full repaint, which
    wipes ``stray`` — text written over the composer by an in-process plugin.
    """

    def __init__(
        self,
        *,
        width=100,
        height=30,
        draft="",
        agent=AGENT,
        stray=None,
        attached=0,
        in_mode=0,
        visible=1,
        render_captures=1,
        swallow=False,
    ):
        self.width = width
        self.height = height
        self.draft = draft
        self.agent = agent
        self.stray = stray
        self.attached = attached
        self.in_mode = in_mode
        self.visible = visible
        self.render_captures = render_captures
        self.swallow = swallow
        self.transcript = list(TRANSCRIPT)
        self.submitted: list[str] = []
        self.shell_commands: list[str] = []
        self.mutations: list[tuple] = []
        self.repaints = 0
        self.environment = {"AQ_READY_PREFIX": BAR, "AQ_SKIP_ESCAPE": "1", "AQ_CLEAR_KEYS": "C-u"}
        self._incoming = ""
        self._shown = 0
        self._shrunk = False

    def _input_rows(self) -> list[str]:
        chunk = self.width - 5
        return [self.draft[i : i + chunk] for i in range(0, len(self.draft), chunk)] or [""]

    def screen(self) -> tuple[list[str], int, int]:
        rows = self._input_rows()
        box = (
            ["  ┃"]
            + [f"  ┃  {row}" if row else "  ┃" for row in rows]
            + ["  ┃", f"  ┃  {self.agent}", "  ╹" + "▀" * (self.width - 3)]
            + ["   ~/repo:main                                   tab agents  ctrl+p commands", ""]
        )
        top = self.height - len(box)
        lines = self.transcript[-top:] + [""] * max(0, top - len(self.transcript)) + box
        cursor_x, cursor_y = 5 + len(rows[-1]), top + len(rows)
        if self.stray is not None:
            # Painted at the cursor, wrapping at the pane edge onto the row
            # below; the trailing newline leaves the cursor a row further on.
            painted = f"  ┃  {self.stray}"
            first, rest = painted[: self.width], painted[self.width :]
            lines[top + 1] = first
            if rest:
                lines[top + 2] = rest + lines[top + 2][len(rest) :]
            cursor_x, cursor_y = 0, top + (3 if rest else 2)
        return lines, cursor_x, cursor_y

    def _render_typed(self) -> None:
        if not self._incoming or self.swallow:
            return
        step = -(-len(self._incoming) // self.render_captures)
        self._shown = min(len(self._incoming), self._shown + step)
        if self._shown == len(self._incoming):
            self.draft += self._incoming
            self._incoming, self._shown = "", 0

    def _visible_draft(self) -> str:
        return self.draft + self._incoming[: self._shown]

    async def tmux(self, *args, stdin=None, **kwargs):
        command = args[0]
        if command == "show-environment":
            key = args[-1]
            return f"{key}={self.environment[key]}\n" if key in self.environment else f"-{key}\n"
        if command == "set-environment":
            if "-u" in args:
                self.environment.pop(args[-1], None)
            else:
                self.environment[args[-2]] = args[-1]
            return ""
        if command == "display-message":
            _, cursor_x, cursor_y = self.screen()
            values = {
                "cursor_x": cursor_x,
                "cursor_y": cursor_y,
                "pane_width": self.width,
                "pane_height": self.height,
                "window_height": self.height,
                "cursor_flag": self.visible,
                "pane_in_mode": self.in_mode,
                "session_attached": self.attached,
            }
            result = args[-1]
            for key, value in values.items():
                result = result.replace("#{" + key + "}", str(value))
            return result + "\n"
        if command == "capture-pane":
            self._render_typed()
            shown, self.draft = self.draft, self._visible_draft()
            try:
                lines, _, _ = self.screen()
            finally:
                self.draft = shown
            return render(lines)
        self.mutations.append(args)
        if command == "resize-window":
            if int(args[-1]) < self.height:
                self._shrunk = True
            elif self._shrunk:
                self._shrunk = False
                self.stray = None
                self.repaints += 1
        elif command == "send-keys" and "-l" in args:
            self.stray = None
            self._incoming += args[-1]
        elif command == "send-keys" and args[-1] == "Enter":
            if self.agent == "Shell":
                self.shell_commands.append(self.draft)
            else:
                self.submitted.append(self.draft)
                self.transcript += ["  ┃", f"  ┃  {self.draft[:60]}", "  ┃", ""]
            self.draft = ""
        elif command == "send-keys" and args[-1] == "C-u":
            self.draft = ""
        return ""


def provider_for(pane: OpenCodePane) -> TmuxProvider:
    provider = TmuxProvider()
    provider.nudge_debounce_ms = 0
    provider._tmux = pane.tmux
    provider._fenced = AsyncMock(return_value=True)
    provider._process_names_hint = AsyncMock(return_value=("opencode",))
    provider._find_agent_pane = AsyncMock(return_value="%1")
    provider._raw_activity = AsyncMock(return_value=100.0)
    return provider


def handle() -> SessionHandle:
    return SessionHandle(provider="tmux", name="p-worker-opencode", instance_token="token")


def resizes(pane: OpenCodePane) -> list[tuple]:
    return [args for args in pane.mutations if args[0] == "resize-window"]


def keys(pane: OpenCodePane) -> list[tuple]:
    return [args for args in pane.mutations if args[0] == "send-keys"]


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(tmux_module, "_SUBMIT_POLLS", ((0.0,),) * 4)
    monkeypatch.setattr(tmux_module, "_REPAINT_SETTLE_POLLS", (0.0, 0.0))
    monkeypatch.setattr(tmux_module, "_LANDED_POLL_SECONDS", 0.0)


def render(lines: list[str]) -> str:
    """Rows as ``capture-pane -p`` prints them: one newline per row."""
    return "".join(f"{line}\n" for line in lines)


def is_empty(pane: OpenCodePane) -> bool:
    lines, x, y = pane.screen()
    return _composer_is_empty(render(lines), BAR, x, y, pane.height, cursor_visible=True)


class TestBoxRecognition:
    def test_an_idle_box_is_an_empty_composer(self):
        assert is_empty(OpenCodePane())

    def test_the_prefix_as_the_harness_file_spells_it_is_recognised(self):
        lines, x, y = OpenCodePane().screen()
        screen = render(lines)
        assert _composer_is_empty(screen, " ┃ ", x, y, 30)

    @pytest.mark.parametrize(
        "pane",
        [
            OpenCodePane(draft="my own half-typed draft"),
            OpenCodePane(draft="x" * 180),
            OpenCodePane(stray=FAST_JEV),
            OpenCodePane(stray="short plugin line"),
            OpenCodePane(agent="Shell"),
        ],
        ids=["draft", "wrapped-draft", "fast-jev-line", "short-stray-line", "shell-mode"],
    )
    def test_text_in_the_box_or_shell_mode_is_not_empty(self, pane):
        assert not is_empty(pane)

    def test_a_hidden_cursor_or_one_off_the_input_column_is_not_empty(self):
        lines, x, y = OpenCodePane().screen()
        screen = render(lines)
        assert _composer_is_empty(screen, BAR, x, y, 30)
        assert not _composer_is_empty(screen, BAR, x, y, 30, cursor_visible=False)
        assert not _composer_is_empty(screen, BAR, x - 2, y, 30)
        assert not _composer_is_empty(screen, BAR, x, y - 1, 30)

    def test_a_multiline_draft_with_the_cursor_on_its_blank_last_row_is_not_empty(self):
        lines, x, y = OpenCodePane().screen()
        # Shift+Enter after one line: the cursor sits on a fresh blank row
        # below text, exactly where an empty composer's cursor would be.
        lines.insert(y, "  ┃  first line")
        del lines[0]
        assert not _composer_is_empty(render(lines), BAR, x, y, 30)

    def test_the_welcome_screens_placeholder_fails_closed(self):
        # OpenCode's start screen (no session yet) paints a grey placeholder
        # on the input row; it is not told apart from a draft.
        welcome = [""] * 17 + [
            "                       ┃",
            '                       ┃  Ask anything… "Fix a TODO in the codebase"',
            "                       ┃",
            "                       ┃  Build · Qwen3.8 27B (local) Ollama (local)",
            "                       ╹" + "▀" * 74,
        ] + [""] * 8
        assert not _composer_is_empty(render(welcome), BAR, 26, 18, 30)
        welcome[18] = "                       ┃"
        assert _composer_is_empty(render(welcome), BAR, 26, 18, 30)

    def test_a_box_whose_top_touches_the_transcript_fails_closed(self):
        lines, x, y = OpenCodePane().screen()
        lines[y - 2] = "  ┃  transcript text directly above"
        assert not _composer_is_empty(render(lines), BAR, x, y, 30)


class TestInputRegion:
    """The typed text lives inside the box, above the agent row."""

    def typed_screen(self, text=REMINDER) -> str:
        pane = OpenCodePane(draft=text)
        return render(pane.screen()[0])

    def test_a_wrapped_reminder_in_the_box_is_pending(self):
        # Every wrapped row carries the bar, which a whitespace-only squash
        # left between the marker's halves.
        assert _submit_pending(self.typed_screen(), _marker_for(REMINDER), BAR) is True
        assert _marker_on_input_line(self.typed_screen(), _marker_for(REMINDER), BAR) is True

    def test_the_transcript_echo_above_an_empty_box_is_submitted(self):
        pane = OpenCodePane()
        pane.transcript += ["  ┃", f"  ┃  {REMINDER[-60:]}", "  ┃", ""]
        screen = render(pane.screen()[0])
        assert _submit_pending(screen, _marker_for(REMINDER), BAR) is False
        assert _marker_on_input_line(screen, _marker_for(REMINDER), BAR) is False

    def test_no_recognisable_box_keeps_a_visible_marker_pending(self):
        pane = OpenCodePane(draft=REMINDER)
        lines = pane.screen()[0]
        border = next(i for i, line in enumerate(lines) if line.strip().startswith("╹"))
        del lines[border]
        assert _submit_pending(render(lines), _marker_for(REMINDER), BAR) is True
        assert _marker_on_input_line(render(lines), _marker_for(REMINDER), BAR) is False

    def test_the_preview_shows_what_was_painted_over_the_box(self):
        pane = OpenCodePane(stray=FAST_JEV)
        preview = _composer_preview(render(pane.screen()[0]), BAR)
        assert preview.startswith("[fast-jev:opencode 2026-09-27T10:14:22.594Z]")
        assert "dropped=56" in preview


class TestNudge:
    async def test_an_idle_box_gets_the_reminder_typed_and_submitted(self):
        pane = OpenCodePane()
        await provider_for(pane).nudge(handle(), REMINDER)
        assert pane.submitted == [REMINDER]
        assert resizes(pane) == []
        assert "AQ_PENDING_SUBMIT" not in pane.environment

    async def test_plugin_output_over_the_box_is_repainted_away_then_nudged(self):
        pane = OpenCodePane(stray=FAST_JEV)
        await provider_for(pane).nudge(handle(), REMINDER)
        assert pane.repaints == 1
        assert resizes(pane) == [
            ("resize-window", "-t", "=p-worker-opencode:", "-y", "29"),
            ("resize-window", "-t", "=p-worker-opencode:", "-y", "30"),
        ]
        # resize-window pins the window to manual sizing; start's ``latest``
        # is put back so an attaching operator still sizes the window.
        assert ("set-option", "-w", "-t", "=p-worker-opencode:", "window-size", "latest") in (
            pane.mutations
        )
        assert pane.submitted == [REMINDER]

    async def test_a_draft_survives_the_repaint_and_the_nudge_defers(self):
        pane = OpenCodePane(draft="my own half-typed draft")
        with pytest.raises(NotSubmitted):
            await provider_for(pane).nudge(handle(), REMINDER)
        assert pane.repaints == 1
        assert pane.draft == "my own half-typed draft"
        assert keys(pane) == []
        assert pane.submitted == []

    async def test_a_draft_is_repainted_at_most_once_a_minute(self):
        pane = OpenCodePane(draft="my own half-typed draft")
        provider = provider_for(pane)
        for _ in range(3):
            with pytest.raises(NotSubmitted):
                await provider.nudge(handle(), REMINDER)
        assert pane.repaints == 1

    async def test_shell_mode_is_never_typed_into(self):
        pane = OpenCodePane(agent="Shell")
        with pytest.raises(NotSubmitted):
            await provider_for(pane).nudge(handle(), REMINDER)
        assert keys(pane) == []
        assert pane.shell_commands == []

    @pytest.mark.parametrize(
        "pane",
        [OpenCodePane(stray=FAST_JEV, attached=1), OpenCodePane(stray=FAST_JEV, in_mode=1)],
        ids=["attached", "copy-mode"],
    )
    async def test_an_attached_or_scrolled_pane_is_left_alone(self, pane):
        with pytest.raises(NotSubmitted):
            await provider_for(pane).nudge(handle(), REMINDER)
        assert pane.mutations == []

    async def test_a_composer_still_rendering_the_typed_text_is_waited_for(self):
        # ~2 s for a 330-character reminder on OpenCode 1.18.32: longer than
        # the eight quiet polls that sufficed for Claude and Codex.
        pane = OpenCodePane(render_captures=14)
        await provider_for(pane).nudge(handle(), REMINDER)
        assert pane.submitted == [REMINDER]

    async def test_swallowed_input_still_gives_up(self):
        pane = OpenCodePane(swallow=True)
        with pytest.raises(NotSubmitted, match="never rendered"):
            await provider_for(pane).nudge(handle(), REMINDER)
        assert pane.submitted == []

    async def test_a_nudge_left_in_the_box_is_resubmitted_exactly(self):
        pane = OpenCodePane()
        provider = provider_for(pane)
        record = tmux_module._PendingSubmit(
            instance_token=handle().instance_token, marker=_marker_for(REMINDER), text=REMINDER,
        )
        await provider._remember_pending(handle(), record)
        pane.draft = REMINDER
        detail = await provider.pending_submit_detail(handle())
        assert detail == {"marker": _marker_for(REMINDER), "observable": True}
        assert await provider.resubmit_pending(handle()) is True
        assert pane.submitted == [REMINDER]

    async def test_an_edited_nudge_in_the_box_is_never_resubmitted(self):
        pane = OpenCodePane()
        provider = provider_for(pane)
        record = tmux_module._PendingSubmit(
            instance_token=handle().instance_token, marker=_marker_for(REMINDER), text=REMINDER,
        )
        await provider._remember_pending(handle(), record)
        pane.draft = "please also " + REMINDER
        assert await provider.resubmit_pending(handle()) is False
        assert pane.submitted == []


class TestComposerProbe:
    async def test_the_probe_names_painted_text_without_touching_the_pane(self):
        pane = OpenCodePane(stray=FAST_JEV)
        probe = await provider_for(pane).composer_probe(handle())
        assert probe["ready"] is False
        assert "has a draft" in probe["reason"]
        assert probe["input"].startswith("[fast-jev:opencode")
        assert pane.mutations == []

    async def test_an_idle_box_probes_ready(self):
        probe = await provider_for(OpenCodePane()).composer_probe(handle())
        assert probe["ready"] is True
        assert probe["reason"] is None

    async def test_a_gone_session_probes_none(self):
        provider = provider_for(OpenCodePane())
        provider._fenced = AsyncMock(return_value=False)
        assert await provider.composer_probe(handle()) is None
