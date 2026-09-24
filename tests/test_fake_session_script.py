"""The fake session provider's file-driven script (provider-failover D23).

The end-to-end kit's daemon runs in another process, so it cannot reach the
in-memory ``script_startup_dialog`` knob; ``sessions.fake_script_file`` names
a JSON file the fake provider re-reads on every start instead, and the same
file answers the login probe for the harnesses it names.  No DB, no CLI.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.config import load_config
from src.sessions.exit_classifier import RATE_LIMIT_PATTERNS
from src.sessions.fake import FakeProvider
from src.sessions.fake_script import (
    RATE_LIMIT_LINE,
    probe_answer,
    read_script,
    script_file_for,
)
from src.sessions.provider import SessionDiedDuringStartup, SessionSpec


def _config(path, provider="fake"):
    return SimpleNamespace(sessions=SimpleNamespace(provider=provider, fake_script_file=str(path)))


def _spec(name, command="prova"):
    return SessionSpec(session_name=name, work_dir="/tmp", command=(f"/usr/bin/{command}", "--x"))


def _write(path, script):
    path.write_text(json.dumps(script))


def test_read_script_accepts_strings_and_objects_and_drops_the_rest(tmp_path):
    path = tmp_path / "script.json"
    _write(
        path,
        {
            "prova": "login_required",
            "provb": {"mode": "rate_limit_midtask", "after_s": 2},
            "provc": "sideways",  # unknown mode: ignored, never guessed
            "provd": 7,
        },
    )
    script = read_script(str(path))
    assert set(script) == {"prova", "provb"}
    assert script["prova"].mode == "login_required"
    assert (script["provb"].mode, script["provb"].after_s) == ("rate_limit_midtask", 2.0)


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]"])
def test_a_missing_or_malformed_script_is_empty(tmp_path, content):
    path = tmp_path / "script.json"
    if content is not None:
        path.write_text(content)
    assert read_script(str(path)) == {}
    assert read_script("") == {}


def test_script_file_is_read_only_by_a_fake_provider_daemon(tmp_path):
    assert script_file_for(_config(tmp_path / "s.json")) == str(tmp_path / "s.json")
    assert script_file_for(_config(tmp_path / "s.json", provider="tmux")) == ""
    assert script_file_for(SimpleNamespace()) == ""


def test_probe_answers_only_for_the_harnesses_the_script_names(tmp_path):
    path = tmp_path / "script.json"
    _write(path, {"prova": "login_required", "provb": "usage_limit"})
    assert probe_answer(str(path), "prova") == "not_authenticated"
    # An exhausted account is still logged in.
    assert probe_answer(str(path), "provb") == "authenticated"
    assert probe_answer(str(path), "claude") is None


async def test_login_required_and_usage_limit_die_on_their_dialogs(tmp_path):
    path = tmp_path / "script.json"
    _write(path, {"prova": "login_required", "provb": "usage_limit"})
    fake = FakeProvider(config=_config(path))

    with pytest.raises(SessionDiedDuringStartup) as auth:
        await fake.start(_spec("s1", "prova"))
    assert (auth.value.dialog, auth.value.signal) == ("login-required", "auth")

    with pytest.raises(SessionDiedDuringStartup) as usage:
        await fake.start(_spec("s2", "provb"))
    assert (usage.value.dialog, usage.value.signal) == ("usage-limit", "usage")
    assert len(fake.dialog_deaths) == 2 and not fake.sessions


async def test_crash_is_an_unlabelled_startup_death(tmp_path):
    path = tmp_path / "script.json"
    _write(path, {"prova": "crash"})
    fake = FakeProvider(config=_config(path))
    with pytest.raises(SessionDiedDuringStartup) as crash:
        await fake.start(_spec("s1"))
    assert crash.value.dialog is None


async def test_the_file_is_reread_on_every_start(tmp_path):
    """The kit restores a provider by rewriting one file -- no restart."""
    path = tmp_path / "script.json"
    _write(path, {"prova": "login_required"})
    fake = FakeProvider(config=_config(path))
    with pytest.raises(SessionDiedDuringStartup):
        await fake.start(_spec("s1"))
    _write(path, {"prova": "ok"})
    handle = await fake.start(_spec("s2"))
    assert await fake.is_running(handle)
    path.unlink()
    assert await fake.is_running(await fake.start(_spec("s3")))


async def test_rate_limit_midtask_exits_with_a_rate_limit_pane(tmp_path):
    import re

    path = tmp_path / "script.json"
    _write(path, {"prova": {"mode": "rate_limit_midtask", "after_s": 0}})
    fake = FakeProvider(config=_config(path))
    handle = await fake.start(_spec("s1"))
    assert not await fake.process_alive(handle)
    pane = await fake.peek(handle)
    assert RATE_LIMIT_LINE in pane
    assert re.search("|".join(RATE_LIMIT_PATTERNS), pane, re.IGNORECASE)


async def test_an_unscripted_harness_and_a_non_fake_config_start_normally(tmp_path):
    path = tmp_path / "script.json"
    _write(path, {"prova": "login_required"})
    assert await FakeProvider(config=_config(path)).start(_spec("s1", "claude"))
    # Only a fake-provider daemon reads the file.
    assert await FakeProvider(config=_config(path, provider="tmux")).start(_spec("s2"))
    assert await FakeProvider().start(_spec("s3"))


async def test_confirm_stopped_uses_live_registry_and_withholds_on_same_name_successor():
    fake = FakeProvider()
    old = await fake.start(_spec("worker"))
    assert not await fake.confirm_stopped(old)
    await fake.stop(old)
    assert await fake.confirm_stopped(old)
    await fake.start(_spec("worker"))
    assert not await fake.confirm_stopped(old)


def test_the_config_key_loads(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
messaging_platform: none
data_dir: {tmp_path}
database:
  url: postgresql+asyncpg://localhost/aq_test
sessions:
  provider: fake
  fake_script_file: {tmp_path}/script.json
"""
    )
    loaded = load_config(str(cfg))
    assert loaded.sessions.fake_script_file == f"{tmp_path}/script.json"
    assert script_file_for(loaded) == f"{tmp_path}/script.json"
