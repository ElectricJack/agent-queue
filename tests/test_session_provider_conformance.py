"""Semantics every :class:`SessionProvider` must share.

Parametrized over the providers available on this host.  **Adding tmux is
one entry in :data:`PROVIDER_CASES`** — a case carries its own marks, so the
tmux row lands with ``pytest.mark.tmux`` and the suite stays green on a host
without a tmux server (and on Windows) without any other change here.

What is pinned:

* start/stop idempotence, and that stop is instance-token fenced
* ``is_running`` vs ``process_alive`` are separate questions
* ``list_running`` filters by prefix and raises ``PartialListError``
  carrying its partial result rather than returning a short list
* meta round-trip (the drain-ack handshake rides on it)
* ``NotSubmitted`` is raised, not swallowed
* unsupported capabilities raise ``CapabilityUnsupported`` rather than
  silently no-op'ing
* nudging does not ratchet ``last_activity`` (poke discounting)

See docs/specs/implementation/session-runtime.md §8.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import pytest

from src.sessions.fake import FakeProvider
from src.sessions.provider import (
    Cap,
    CapabilityUnsupported,
    NotSubmitted,
    PartialListError,
    SessionHandle,
    SessionSpec,
)
from src.sessions.subprocess import SubprocessProvider


@dataclass
class ProviderCase:
    """One provider under test, plus how to make it produce a live session."""

    id: str
    factory: object
    #: argv that stays alive until killed.
    long_running: tuple[str, ...]
    marks: tuple = field(default_factory=tuple)


def _python_sleeper() -> tuple[str, ...]:
    # A real child that outlives the test body, without depending on
    # `sleep` existing (it does not, on Windows).
    return (sys.executable, "-c", "import time; time.sleep(30)")


PROVIDER_CASES: list[ProviderCase] = [
    ProviderCase(id="fake", factory=FakeProvider, long_running=("agent",)),
    ProviderCase(
        id="subprocess",
        factory=SubprocessProvider,
        long_running=_python_sleeper(),
    ),
    # tmux joins here when its provider lands:
    #   ProviderCase(id="tmux", factory=TmuxProvider,
    #                long_running=_python_sleeper(),
    #                marks=(pytest.mark.tmux,))
]


def _params():
    return [pytest.param(c, id=c.id, marks=c.marks) for c in PROVIDER_CASES]


@pytest.fixture(params=_params())
def case(request) -> ProviderCase:
    return request.param


@pytest.fixture
def provider(case, tmp_path):
    class _Cfg:
        data_dir = str(tmp_path / "state")
        security = None

    return case.factory(config=_Cfg())


def _spec(case, tmp_path, name="s-t1", token="tok-1") -> SessionSpec:
    return SessionSpec(
        session_name=name,
        work_dir=str(tmp_path / "wd"),
        command=case.long_running,
        env={"AQ_SESSION_ID": "sess-1", "AQ_INSTANCE_TOKEN": token},
        prompt=None,
        prompt_mode="none",
        process_names=("python", "agent"),
        instance_token=token,
    )


class TestLifecycle:
    async def test_start_returns_a_handle_carrying_the_instance_token(
        self, provider, case, tmp_path
    ):
        handle = await provider.start(_spec(case, tmp_path))
        try:
            assert handle.name == "s-t1"
            assert handle.provider == provider.name
            assert handle.instance_token == "tok-1"
        finally:
            await provider.stop(handle)

    async def test_stop_is_idempotent(self, provider, case, tmp_path):
        handle = await provider.start(_spec(case, tmp_path))
        await provider.stop(handle)
        # A second stop must not raise — the reconciler can race a drain.
        await provider.stop(handle)
        assert await provider.is_running(handle) is False

    async def test_stop_is_instance_token_fenced(self, provider, case, tmp_path):
        """A handle for a predecessor must never kill its successor."""
        live = await provider.start(_spec(case, tmp_path, token="tok-new"))
        stale = SessionHandle(name="s-t1", provider=provider.name, instance_token="tok-old")
        try:
            await provider.stop(stale)
            assert await provider.is_running(live) is True
        finally:
            await provider.stop(live)

    async def test_interrupt_does_not_raise_on_a_live_session(
        self, provider, case, tmp_path
    ):
        handle = await provider.start(_spec(case, tmp_path))
        try:
            await provider.interrupt(handle)
        finally:
            await provider.stop(handle)

    async def test_interrupt_on_a_gone_session_is_a_no_op(self, provider, case, tmp_path):
        handle = await provider.start(_spec(case, tmp_path))
        await provider.stop(handle)
        await provider.interrupt(handle)


class TestObservation:
    async def test_is_running_and_process_alive_are_both_true_when_live(
        self, provider, case, tmp_path
    ):
        handle = await provider.start(_spec(case, tmp_path))
        try:
            assert await provider.is_running(handle) is True
            assert await provider.process_alive(handle, ("python", "agent")) is True
        finally:
            await provider.stop(handle)

    async def test_both_are_false_after_stop(self, provider, case, tmp_path):
        handle = await provider.start(_spec(case, tmp_path))
        await provider.stop(handle)
        assert await provider.is_running(handle) is False
        assert await provider.process_alive(handle, ("python", "agent")) is False

    async def test_list_running_filters_by_prefix(self, provider, case, tmp_path):
        task = await provider.start(_spec(case, tmp_path, name="s-t1", token="a"))
        named = await provider.start(_spec(case, tmp_path, name="n-supervisor", token="b"))
        try:
            assert [h.name for h in await provider.list_running("s-")] == ["s-t1"]
            assert [h.name for h in await provider.list_running("n-")] == ["n-supervisor"]
            assert len(await provider.list_running("")) == 2
        finally:
            await provider.stop(task)
            await provider.stop(named)

    async def test_list_running_excludes_stopped_sessions(self, provider, case, tmp_path):
        handle = await provider.start(_spec(case, tmp_path))
        await provider.stop(handle)
        assert await provider.list_running("s-") == []

    async def test_last_activity_is_a_timestamp_or_none(self, provider, case, tmp_path):
        handle = await provider.start(_spec(case, tmp_path))
        try:
            seen = await provider.last_activity(handle)
            assert seen is None or isinstance(seen, float)
        finally:
            await provider.stop(handle)


class TestCapabilityGating:
    async def test_supports_matches_the_declared_capability_set(self, provider):
        for cap in Cap:
            assert provider.supports(cap) == (cap in provider.capabilities)

    async def test_nudge_raises_when_unsupported(self, provider, case, tmp_path):
        if provider.supports(Cap.NUDGE):
            pytest.skip("provider supports NUDGE")
        handle = await provider.start(_spec(case, tmp_path))
        try:
            with pytest.raises(CapabilityUnsupported):
                await provider.nudge(handle, "hello")
        finally:
            await provider.stop(handle)

    async def test_attach_raises_when_unsupported(self, provider, case, tmp_path):
        if provider.supports(Cap.ATTACH):
            pytest.skip("provider supports ATTACH")
        handle = await provider.start(_spec(case, tmp_path))
        try:
            with pytest.raises(CapabilityUnsupported):
                await provider.attach_command(handle)
        finally:
            await provider.stop(handle)

    async def test_peek_returns_a_string_when_supported(self, provider, case, tmp_path):
        if not provider.supports(Cap.PEEK):
            pytest.skip("provider has no PEEK")
        handle = await provider.start(_spec(case, tmp_path))
        try:
            assert isinstance(await provider.peek(handle, 10), str)
        finally:
            await provider.stop(handle)


class TestMeta:
    async def test_meta_round_trips(self, provider, case, tmp_path):
        """The drain-ack handshake rides on this."""
        handle = await provider.start(_spec(case, tmp_path))
        try:
            assert await provider.get_meta(handle, "AQ_DRAIN_ACK") is None
            await provider.set_meta(handle, "AQ_DRAIN_ACK", "1")
            assert await provider.get_meta(handle, "AQ_DRAIN_ACK") == "1"
        finally:
            await provider.stop(handle)

    async def test_meta_is_fenced_by_instance_token(self, provider, case, tmp_path):
        handle = await provider.start(_spec(case, tmp_path, token="tok-new"))
        stale = SessionHandle(name="s-t1", provider=provider.name, instance_token="tok-old")
        try:
            await provider.set_meta(stale, "AQ_DRAIN_ACK", "1")
            assert await provider.get_meta(handle, "AQ_DRAIN_ACK") is None
        finally:
            await provider.stop(handle)


# ---------------------------------------------------------------------------
# Semantics only a provider that advertises NUDGE / partial listing can show.
# ---------------------------------------------------------------------------


class TestFakeSpecificSemantics:
    """Pinned on FakeProvider because it can script the failure modes.

    These are still *conformance* rules — tmux must behave the same way —
    but only a scriptable provider can produce them on demand.
    """

    @pytest.fixture
    def fake(self, tmp_path):
        return FakeProvider()

    async def test_not_submitted_is_raised_not_swallowed(self, fake, tmp_path):
        case = PROVIDER_CASES[0]
        handle = await fake.start(_spec(case, tmp_path))
        fake.swallow_next_nudge(handle.name)
        with pytest.raises(NotSubmitted):
            await fake.nudge(handle, "are you alive?")
        # The failure is per-attempt, not sticky.
        await fake.nudge(handle, "second try")
        assert fake.sent_nudges == [(handle.name, "second try")]

    async def test_partial_list_error_carries_what_was_enumerated(self, fake, tmp_path):
        case = PROVIDER_CASES[0]
        await fake.start(_spec(case, tmp_path, name="s-a", token="a"))
        await fake.start(_spec(case, tmp_path, name="s-b", token="b"))
        fake.script_partial_list(RuntimeError("no server"))
        with pytest.raises(PartialListError) as excinfo:
            await fake.list_running("s-")
        # Unknown is not dead: the caller gets the partial set *and* a
        # signal that it is incomplete, so it can defer rather than reap.
        assert {h.name for h in excinfo.value.partial} == {"s-a", "s-b"}
        assert isinstance(excinfo.value.cause, RuntimeError)

    async def test_nudging_does_not_ratchet_activity(self, fake, tmp_path):
        """Poke discounting: our own send must not look like agent progress."""
        case = PROVIDER_CASES[0]
        handle = await fake.start(_spec(case, tmp_path))
        before = await fake.last_activity(handle)
        await fake.nudge(handle, "status?")
        assert await fake.last_activity(handle) == before

    async def test_scripted_death_flips_process_alive_only(self, fake, tmp_path):
        """A pane can outlive its agent — that is why the two probes differ."""
        case = PROVIDER_CASES[0]
        handle = await fake.start(_spec(case, tmp_path))
        fake.script_death(handle.name)
        assert await fake.process_alive(handle, ()) is False
        assert await fake.is_running(handle) is True
