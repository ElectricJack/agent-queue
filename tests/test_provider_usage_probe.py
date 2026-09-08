# tests/test_provider_usage_probe.py
"""``provider_usage_probe`` — implementation spec T4.

The probe is the only *active* half of provider usage, so almost everything
here is about what it does when the subprocess misbehaves: the last good
reading has to survive a timeout, a crash, an unreadable body and a CLI whose
wording moved, because a frozen-but-labelled number beats a wrong one and a
wrong one is what a "best effort" write would produce.

The subprocess is faked at ``asyncio.create_subprocess_exec`` rather than by
installing a shell script, so the assertions can also see the argv — in
particular that ``--bare`` is never on it.  ``--bare`` forces API-key auth and
would report a completely different account.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.commands.contracts import CONTRACTS
from src.database.queries.provider_usage_queries import probe_health_key
from src.providers import probe as probe_module

# The verified body of ``claude -p "/usage" --output-format json`` on
# claude 2.1.263, including the "what's contributing" block that follows the
# limit lines and is full of percentages that are *not* limits (amendment A2).
VERIFIED_RESULT = """You are currently using your subscription to power your Claude Code usage

Current session: 5% used · resets Sep 7, 3:59pm (America/Los_Angeles)
Current week (all models): 45% used · resets Sep 9, 2:59pm (America/Los_Angeles)
Current week (Fable): 81% used · resets Sep 9, 2:59pm (America/Los_Angeles)

What's contributing to your limits usage?
Approximate, based on local sessions on this machine.

Last 24h · 2240 requests · 72 sessions
  64% of your usage was at >150k context
  32% of your usage came from subagent-heavy sessions
  Top subagents: fork 14%, general-purpose 6%
"""

VERIFIED_ENVELOPE = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 0,
        "total_cost_usd": 0,
        "result": VERIFIED_RESULT,
    }
)


class FakeProcess:
    """Just enough of ``asyncio.subprocess.Process`` for the probe.

    ``hang=True`` never completes, which is how the timeout path is exercised
    without waiting twenty real seconds; ``killed`` records that the probe
    reaped the child it abandoned.
    """

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
                 hang: bool = False) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False
        self.started = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        if self._hang:
            await asyncio.Event().wait()
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def spawned(monkeypatch):
    """Replace ``create_subprocess_exec``; return the recorded launches."""
    launches: list[tuple[tuple, dict]] = []
    box: dict = {"process": FakeProcess(stdout=VERIFIED_ENVELOPE.encode()), "raises": None}

    async def fake_exec(*argv, **kwargs):
        launches.append((argv, kwargs))
        if box["raises"] is not None:
            raise box["raises"]
        return box["process"]

    monkeypatch.setattr(probe_module.asyncio, "create_subprocess_exec", fake_exec)
    return {"launches": launches, "box": box}


@pytest.fixture
async def handler(command_handler_factory, spawned):
    return await command_handler_factory()


@pytest.fixture
def contracted(handler):
    """Point the contract adapters at this test's handler, then unwire them."""
    from src.commands.contracts.builtin import set_handler_provider

    set_handler_provider(lambda: handler)
    yield handler
    set_handler_provider(None)


async def _snapshot_rows(handler) -> list[dict]:
    return await handler.db.latest_provider_usage()


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_verified_body_writes_three_probe_snapshots(handler, spawned) -> None:
    result = await handler.execute("provider_usage_probe", {"provider": "claude"})

    assert result["success"] is True
    assert result["outcome"] == "probed"
    assert result["recorded"] == 3
    assert result["unparsed"] is False

    rows = await _snapshot_rows(handler)
    assert {(row["window"], row["scope"], row["used_percent"]) for row in rows} == {
        ("session", "", 5.0),
        ("week", "all models", 45.0),
        ("week", "Fable", 81.0),
    }
    assert {row["source"] for row in rows} == {"probe"}
    assert {row["provider"] for row in rows} == {"claude"}
    # The "64% of your usage was at >150k context" line is not a limit line.
    assert 64.0 not in {row["used_percent"] for row in rows}


async def test_the_probe_never_passes_bare(handler, spawned) -> None:
    """``--bare`` forces API-key auth and would report the wrong account."""
    await handler.execute("provider_usage_probe", {})

    (argv, kwargs), = spawned["launches"]
    assert argv[0] == "claude"
    assert list(argv[1:]) == ["-p", "/usage", "--output-format", "json"]
    assert "--bare" not in argv
    # A fixed, safe cwd: the probe must never inherit a worktree.
    assert kwargs["cwd"] == handler.config.data_dir


async def test_the_configured_binary_is_what_runs(handler, spawned) -> None:
    handler.config.providers.claude.binary = "/opt/harness-bin/claude"
    await handler.execute("provider_usage_probe", {})

    (argv, _kwargs), = spawned["launches"]
    assert argv[0] == "/opt/harness-bin/claude"


# ---------------------------------------------------------------------------
# Every failure path: success False, and nothing stored
# ---------------------------------------------------------------------------


async def test_a_timeout_is_unavailable_and_writes_nothing(handler, spawned, monkeypatch) -> None:
    hung = FakeProcess(hang=True)
    spawned["box"]["process"] = hung
    monkeypatch.setattr(probe_module, "DEFAULT_TIMEOUT_SECONDS", 0.05)

    result = await handler.execute("provider_usage_probe", {})

    assert result["success"] is True
    assert result["outcome"] == probe_module.UNAVAILABLE
    assert "did not answer" in result["detail"]
    health = await handler.db.read_probe_health("claude")
    assert health["detail"] == result["detail"]
    assert await _snapshot_rows(handler) == []
    assert hung.killed, "an abandoned probe must be reaped, not left a zombie"


async def test_cancelling_a_probe_reaps_its_subprocess(spawned) -> None:
    hung = FakeProcess(hang=True)
    spawned["box"]["process"] = hung
    task = asyncio.create_task(probe_module.probe_claude_usage())
    await asyncio.wait_for(hung.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert hung.killed


async def test_a_non_zero_exit_fails_and_writes_nothing(handler, spawned) -> None:
    spawned["box"]["process"] = FakeProcess(
        stdout=b"", stderr=b"not logged in\n", returncode=1
    )

    result = await handler.execute("provider_usage_probe", {})

    assert result["success"] is False
    assert result["reason"] == probe_module.CLI_ERROR
    assert "not logged in" in result["error"]
    assert await _snapshot_rows(handler) == []


async def test_an_unreadable_body_fails_and_writes_nothing(handler, spawned) -> None:
    spawned["box"]["process"] = FakeProcess(stdout=b"<html>login</html>")

    result = await handler.execute("provider_usage_probe", {})

    assert result["success"] is False
    assert result["reason"] == probe_module.MALFORMED
    assert await _snapshot_rows(handler) == []


async def test_an_is_error_envelope_fails_and_writes_nothing(handler, spawned) -> None:
    spawned["box"]["process"] = FakeProcess(
        stdout=json.dumps({"is_error": True, "result": "rate limited"}).encode()
    )

    result = await handler.execute("provider_usage_probe", {})

    assert result["success"] is False
    assert result["reason"] == probe_module.CLI_ERROR
    assert await _snapshot_rows(handler) == []


async def test_a_failed_probe_leaves_the_last_good_snapshot_untouched(
    handler, spawned, monkeypatch
) -> None:
    await handler.execute("provider_usage_probe", {})
    before = await _snapshot_rows(handler)
    assert len(before) == 3

    spawned["box"]["process"] = FakeProcess(stderr=b"boom", returncode=2)
    failed = await handler.execute("provider_usage_probe", {})

    assert failed["success"] is False
    assert await _snapshot_rows(handler) == before


# ---------------------------------------------------------------------------
# The three "not a fault" outcomes
# ---------------------------------------------------------------------------


async def test_a_missing_cli_is_a_success_with_nothing_to_report(handler, spawned) -> None:
    """A box without the CLI is a fact about the box, not a broken step."""
    spawned["box"]["raises"] = FileNotFoundError(2, "No such file or directory", "claude")

    result = await handler.execute("provider_usage_probe", {})

    assert result["success"] is True
    assert result["outcome"] == "unavailable"
    assert result["recorded"] == 0
    # Reported as ``detail``: an ``error`` key would classify the contract
    # outcome as ``rejected`` and fail the playbook step every ten minutes.
    assert "error" not in result
    assert await _snapshot_rows(handler) == []


async def test_unparsed_output_stores_nothing_and_says_so(handler, spawned) -> None:
    spawned["box"]["process"] = FakeProcess(
        stdout=json.dumps({"result": "Usage information is unavailable right now."}).encode()
    )

    result = await handler.execute("provider_usage_probe", {})

    assert result["success"] is True
    assert result["outcome"] == "unparsed"
    assert result["unparsed"] is True
    assert await _snapshot_rows(handler) == []


async def test_an_api_key_account_is_not_applicable_rather_than_unparsed(
    handler, spawned
) -> None:
    body = "You are using an API key from console.anthropic.com to power your usage"
    spawned["box"]["process"] = FakeProcess(stdout=json.dumps({"result": body}).encode())

    result = await handler.execute("provider_usage_probe", {})

    assert result["success"] is True
    assert result["outcome"] == "not_applicable"
    assert result["unparsed"] is False
    assert await _snapshot_rows(handler) == []


async def test_a_disabled_probe_runs_no_subprocess(handler, spawned) -> None:
    handler.config.providers.claude.usage_probe_enabled = False

    result = await handler.execute("provider_usage_probe", {})

    assert result["success"] is True
    assert result["outcome"] == "disabled"
    assert spawned["launches"] == []


async def test_an_unknown_provider_is_rejected(handler, spawned) -> None:
    result = await handler.execute("provider_usage_probe", {"provider": "codex"})

    assert result["success"] is False
    assert "unknown provider" in result["error"]
    assert spawned["launches"] == []


# ---------------------------------------------------------------------------
# The probe's own health, which is all T7's doctor check has to read
# ---------------------------------------------------------------------------


async def test_every_probe_records_its_own_health(handler, spawned) -> None:
    await handler.execute("provider_usage_probe", {})
    healthy = await handler.db.read_probe_health("claude")
    assert healthy["ok"] is True
    assert healthy["outcome"] == "probed"
    assert healthy["unparsed"] is False
    assert healthy["recorded"] == 3
    assert healthy["ts"] > 0

    spawned["box"]["process"] = FakeProcess(
        stdout=json.dumps({"result": "nothing that looks like a limit line"}).encode()
    )
    await handler.execute("provider_usage_probe", {})
    moved = await handler.db.read_probe_health("claude")
    assert moved["unparsed"] is True, "the wording moved and the doctor must be able to say so"

    spawned["box"]["process"] = FakeProcess(stderr=b"boom", returncode=3)
    await handler.execute("provider_usage_probe", {})
    broken = await handler.db.read_probe_health("claude")
    assert broken["ok"] is False
    assert broken["outcome"] == probe_module.CLI_ERROR
    assert broken["error"]


async def test_probe_health_lives_under_the_key_the_doctor_reads(handler, spawned) -> None:
    """A7 pins the ``system_config`` key; a typo is a check that never fires."""
    assert probe_health_key("claude") == "providers.claude_usage.last_probe"
    await handler.execute("provider_usage_probe", {})
    assert await handler.db.read_probe_health("claude") is not None


async def test_reading_health_before_any_probe_is_none(handler) -> None:
    assert await handler.db.read_probe_health("claude") is None


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_the_command_is_contracted_for_playbooks() -> None:
    registration = CONTRACTS.get("provider_usage_probe")
    assert registration is not None, "a playbook step cannot call an uncontracted command"

    contract = registration.contract.execution
    successes = {o.name for o in contract.outcomes if o.classification.value == "success"}
    assert successes == {"probed", "unparsed", "not_applicable", "unavailable", "disabled"}
    failures = {o.name for o in contract.outcomes if o.classification.value == "failure"}
    assert failures == {"rejected"}
    assert [clause.subject.value for clause in contract.effects] == ["provider_usage"]


async def test_the_contract_adapter_maps_a_probe_onto_its_outcome(contracted, spawned) -> None:
    from src.commands.contracts.builtin import ProviderUsageProbeArgs

    registration = CONTRACTS.get("provider_usage_probe")
    result = await registration.invoke(ProviderUsageProbeArgs(provider="claude"), None)

    assert result.outcome == "probed"
    assert result.value.recorded == 3
    assert result.value.provider == "claude"


async def test_a_failed_probe_maps_onto_rejected(contracted, spawned) -> None:
    from src.commands.contracts.builtin import ProviderUsageProbeArgs

    spawned["box"]["process"] = FakeProcess(stderr=b"boom", returncode=1)
    registration = CONTRACTS.get("provider_usage_probe")
    result = await registration.invoke(ProviderUsageProbeArgs(), None)

    assert result.outcome == "rejected"
    # The playbook executor round-trips the value before taking an outcome
    # edge; a model_construct bypass must not turn rejection into a fault.
    validated = registration.contract.execution.result_model.model_validate(
        result.value.model_dump()
    )
    assert validated.outcome == "rejected"
    assert validated.provider == "claude"
    from types import SimpleNamespace

    from src.playbooks.definition import CommandStep
    from src.playbooks.executors.command import _consume

    step = CommandStep.model_validate({
        "rule": "probe", "title": "Probe",
        "source": {"path": "probe.md", "start_line": 1, "end_line": 1},
        "command": "provider_usage_probe", "transitions": {"rejected": "failed"},
        "save_result_as": "probe_result",
    })
    consumed = _consume(
        result, registration, step, SimpleNamespace(run_id="probe-run"),
        resolved_inputs={}, key="probe-key",
    )
    assert consumed.outcome == "rejected"
    assert consumed.value["provider"] == "claude"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_provider_config_defaults_and_validation() -> None:
    from src.config import ProvidersConfig

    defaults = ProvidersConfig()
    assert defaults.claude.usage_probe_enabled is True
    assert defaults.claude.binary == "claude"
    # Twice the shipped playbook's ten-minute cadence, plus slack.
    assert defaults.claude.stale_after_seconds == 1500
    assert defaults.codex_stale_after_seconds == 4 * 3600
    assert defaults.validate() == []

    bad = ProvidersConfig()
    bad.claude.binary = "  "
    bad.claude.stale_after_seconds = 0
    bad.codex_stale_after_seconds = -1
    assert len(bad.validate()) == 3


def test_the_providers_section_is_reachable_from_yaml(tmp_path) -> None:
    import yaml

    from src.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(tmp_path / "d"),
                "database": {"url": "postgresql+asyncpg://test:test@localhost/test"},
                "discord": {"bot_token": "t", "guild_id": "1"},
                "providers": {
                    "claude": {"binary": "/opt/claude", "usage_probe_enabled": False},
                    "codex_stale_after_seconds": 60,
                },
            }
        )
    )
    config = load_config(str(path))
    assert config.providers.claude.binary == "/opt/claude"
    assert config.providers.claude.usage_probe_enabled is False
    assert config.providers.codex_stale_after_seconds == 60
    # Unset keys keep their code default rather than being zeroed.
    assert config.providers.claude.stale_after_seconds == 1500
