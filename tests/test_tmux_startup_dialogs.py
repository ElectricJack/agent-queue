"""Startup readiness must not mistake a dialog's menu row for the composer.

Regression cover for the pool-gate failure seen live on Codex 0.151 and
Claude (task smart-orbit.7): fresh workspaces sat on their trust screens
while the session rows already said *running*.  Two defects combined:

* ``_await_ready``'s ready-prefix poll accepted **any** line starting with
  the harness glyph — including the trust menus' own rows, Codex's
  ``› 1. Yes, continue`` and Claude's ``❯ No, exit``; and
* the post-readiness dismissal pass could run a beat *before* the trust
  screen was painted, so nothing ever answered it.

Everything here runs against a scripted fake pane — no tmux server, no
wall-clock guessing — so both glyphs are covered deterministically.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import src.sessions as sessions_pkg
from src.sessions.harness_parser import parse_harness_markdown
from src.sessions.provider import DialogRule, SessionHandle, SessionSpec
from src.sessions.tmux import TmuxProvider

NBSP = " "

#: Claude's composer prompt: ``❯`` + NBSP.  Codex's: ``›`` + a plain space.
CLAUDE_COMPOSER = f"❯{NBSP}"
CODEX_COMPOSER = "› "

CLAUDE_TRUST = """\
 Do you trust the files in this folder?

 /home/agent/work

 ❯ No, exit
   Yes, proceed
"""

CODEX_TRUST = """\
>_ OpenAI Codex (v0.151.0)

  Do you trust the contents of this directory?

› 1. Yes, continue
  2. No, exit
"""


def _shipped_rule(harness_id: str, name: str) -> DialogRule:
    """The rule the daemon actually ships, not a hand-built stand-in.

    A stand-in with ``is_regex=True`` proved the poll answers a trust screen
    while the shipped Claude rule — an alternation *without* the flag —
    could never match one.  Driving the fake pane with the shipped rules
    keeps this file honest about what runs in production.
    """
    path = Path(sessions_pkg.__file__).parent / "default_harnesses" / f"{harness_id}.md"
    parsed = parse_harness_markdown(path.read_text(encoding="utf-8"), fallback_id=harness_id)
    assert parsed.is_valid, parsed.errors
    (rule,) = [r for r in parsed.harness.dialogs if r.name == name]
    return rule


CLAUDE_RULE = _shipped_rule("claude", "trust-folder")
CODEX_RULE = _shipped_rule("codex", "trust-directory")

#: Both harnesses, addressed by the two things that differ: the glyph the
#: readiness poll looks for and the trust screen that starts with it.
HARNESSES = [
    pytest.param(CLAUDE_COMPOSER, CLAUDE_TRUST, CLAUDE_RULE, id="claude"),
    pytest.param(CODEX_COMPOSER, CODEX_TRUST, CODEX_RULE, id="codex"),
]


class _Pane:
    """A fake tmux pane whose frame is a function of what has happened."""

    def __init__(self, script):
        self.script = script
        self.captures = 0
        self.sent: list[tuple[str, ...]] = []
        self.composer_frames = 0
        #: Capture index at which the first keys had been sent, so a script
        #: can say "two frames after the dialog was answered" without a clock.
        self.dismissed_at = 0

    def frame(self) -> str:
        self.captures += 1
        if self.sent and not self.dismissed_at:
            self.dismissed_at = self.captures
        return self.script(self)


def _provider(pane, *, settle=0.4, budget=8):
    provider = TmuxProvider(
        config=SimpleNamespace(
            data_dir="/nonexistent",
            sessions=SimpleNamespace(
                tmux_socket="aq-test",
                dialog_budget_seconds=budget,
                dialog_settle_seconds=settle,
                state_cache_ttl_seconds=2,
                nudge_debounce_ms=500,
            ),
        )
    )

    async def fake_tmux(*args: str, **kwargs) -> str:
        if args[0] == "capture-pane":
            return pane.frame()
        if args[0] == "display-message":
            return "python\n"  # not a shell: phase 1 is satisfied
        if args[0] == "list-panes":
            return "0\n"  # pane_dead == 0
        if args[0] == "send-keys":
            pane.sent.append(tuple(args[3:]))
            return ""
        return ""

    provider._tmux = fake_tmux  # type: ignore[method-assign]
    return provider


def _spec(prefix: str, rule: DialogRule) -> SessionSpec:
    return SessionSpec(
        session_name="s-dialog",
        work_dir="/tmp/aq-fake-wd",
        command=("harness",),
        prompt=None,
        prompt_mode="none",
        ready_delay_ms=1000,
        ready_prompt_prefix=prefix,
        dialogs=(rule,),
    )


async def _await_ready(provider, spec):
    handle = SessionHandle(name=spec.session_name, provider="tmux", instance_token="tok")
    await provider._await_ready(handle, spec)


@pytest.mark.parametrize(("prefix", "trust", "rule"), HARNESSES)
class TestReadyPrefixFalsePositives:
    async def test_trust_menu_row_is_not_the_composer(self, prefix, trust, rule):
        # The trust screen lands *after* the pre-readiness pass has already
        # run, and its highlighted row starts with the very glyph the
        # readiness poll matches.  Startup must recognise the frame as a
        # dialog, answer it, and go on waiting for the real composer — which
        # this harness paints only after a couple of quiet frames.
        def script(pane: _Pane) -> str:
            if pane.captures == 1:
                return "starting…\n"  # pre-readiness pass: nothing to answer
            if not pane.sent:
                return trust
            if pane.captures <= pane.dismissed_at + 2:
                return "Loading configuration…\n"
            pane.composer_frames += 1
            return f"welcome\n{prefix}\n"

        pane = _Pane(script)
        provider = _provider(pane)
        await _await_ready(provider, _spec(prefix, rule))

        assert pane.sent == [("Enter",)], "the trust dialog was never answered"
        assert pane.composer_frames > 0, "startup returned before the composer painted"

    async def test_dialog_painted_after_readiness_is_still_dismissed(self, prefix, trust, rule):
        # The composer is up first and the trust screen lands late — the
        # exact race the old single-shot post-readiness pass lost.  Three
        # captures is one for the pre-readiness pass, one for the readiness
        # poll, and one for the first quiet capture of the final pass.
        late_after = 3

        def script(pane: _Pane) -> str:
            if pane.captures <= late_after or pane.sent:
                pane.composer_frames += 1
                return f"welcome\n{prefix}\n"
            return trust

        pane = _Pane(script)
        provider = _provider(pane)
        await _await_ready(provider, _spec(prefix, rule))

        assert pane.sent == [("Enter",)], "a late trust dialog was left on screen"

    async def test_quiet_pane_still_settles_promptly(self, prefix, trust, rule):
        # No dialog ever appears: startup must not send keys and must not
        # hang past the settle window.
        pane = _Pane(lambda p: f"welcome\n{prefix}\n")
        provider = _provider(pane, settle=0.2)
        await _await_ready(provider, _spec(prefix, rule))

        assert pane.sent == []
