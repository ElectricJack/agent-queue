"""Run ``claude -p "/usage"`` and turn its output into snapshots.

Claude publishes no local rate-limit file the way Codex does, so the only
way to learn what the account has left is to ask the CLI.  That call is
cheap and, more importantly, *free*: a live run reports ``num_turns: 0`` and
``total_cost_usd: 0``, so the probe bills none of the quota it reports.

Two rules shape everything here:

* **Never pass ``--bare``.**  It forces API-key authentication, which reads a
  different account than the one the fleet's sessions use — the answer would
  be confidently wrong rather than missing.
* **A failed probe must cost nothing.**  A timeout, a non-zero exit, an
  unreadable body: each returns a result saying so and writes no snapshot,
  so the last good reading survives every failure mode.  A blank card beats
  a wrong number, and a stale-but-labelled number beats a blank card.

The one failure that is not a fault is a box without the CLI installed:
:data:`UNAVAILABLE` is a fact about the machine, not a broken step, and the
caller reports it as a success so a ten-minute timer does not fill the run
overlay with noise nobody can act on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from src.providers.claude_usage import parse_usage_text
from src.providers.snapshot import ProviderUsageSnapshot

logger = logging.getLogger(__name__)

__all__ = [
    "MALFORMED",
    "NOT_APPLICABLE",
    "PROBED",
    "TIMEOUT",
    "UNAVAILABLE",
    "UNPARSED",
    "ProbeResult",
    "probe_claude_usage",
]

#: The probe read at least one limit line.
PROBED = "probed"
#: The CLI answered, but nothing in the body looked like a limit line — the
#: wording moved under us and the regex needs a human.
UNPARSED = "unparsed"
#: An API-key account has no subscription window to report.  Not a fault.
NOT_APPLICABLE = "not_applicable"
#: The ``claude`` binary is absent or did not answer in time. Not a fault.
UNAVAILABLE = "unavailable"
#: The CLI did not answer inside the timeout.
TIMEOUT = "timeout"
#: The CLI answered with something that is not the JSON envelope we asked for.
MALFORMED = "malformed"
#: The CLI exited non-zero, or its envelope carried ``is_error``.
CLI_ERROR = "cli_error"

#: Wall-clock ceiling on one probe.  A verified live run takes ~3.6s; twenty
#: seconds is generous enough that a loaded box is not mistaken for a hang and
#: short enough that a playbook step never outlives its own timer.
DEFAULT_TIMEOUT_SECONDS = 20.0

_ARGS = ("-p", "/usage", "--output-format", "json")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What one probe learned, and whether the caller may act on it.

    ``ok`` is the caller's ``success``: false only for the outcomes that mean
    "the CLI returned an error or malformed answer". ``detail`` explains an outcome
    the caller reports as a success (a missing binary); ``error`` explains
    one it reports as a failure.  Exactly one of the two is ever set.
    """

    outcome: str
    ok: bool = True
    snapshots: list[ProviderUsageSnapshot] = field(default_factory=list)
    unparsed: bool = False
    error: str | None = None
    detail: str | None = None


async def probe_claude_usage(
    *,
    binary: str = "claude",
    cwd: str | None = None,
    timeout: float | None = None,
    now: float | None = None,
) -> ProbeResult:
    """Ask the Claude CLI what is left of the subscription's windows.

    Args:
        binary: The executable to run.  A box whose harness is a shim points
            ``providers.claude.binary`` at the shim rather than at ``claude``.
        cwd: A fixed, safe working directory — the daemon's data dir in
            production.  The probe must never inherit a worktree.
        timeout: Seconds to wait before giving up and killing the child.
            ``None`` reads :data:`DEFAULT_TIMEOUT_SECONDS` at call time.
        now: Observation epoch; defaults to :func:`time.time`.  Passed in by
            tests so the year-less reset dates resolve deterministically.

    Returns:
        A :class:`ProbeResult`.  It never raises: every failure mode this
        function knows about is one of its outcomes, because the caller runs
        on a timer and has nothing useful to do with an exception.
    """

    observed_at = time.time() if now is None else now
    budget = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *_ARGS,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        # Not every box has the Claude CLI, and the ones that do not are not
        # broken.  Say so and let the caller record a healthy "nothing to
        # report" rather than a failing step every ten minutes.
        return ProbeResult(outcome=UNAVAILABLE, detail=f"{binary}: {exc}")
    except OSError as exc:  # pragma: no cover - defensive
        return ProbeResult(outcome=UNAVAILABLE, detail=f"{binary}: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=budget)
    except asyncio.CancelledError:
        await _terminate(proc)
        raise
    except TimeoutError:
        await _terminate(proc)
        return ProbeResult(
            outcome=UNAVAILABLE,
            detail=f"{binary} /usage did not answer within {budget:g}s",
        )

    if proc.returncode != 0:
        return ProbeResult(
            outcome=CLI_ERROR,
            ok=False,
            error=(
                f"{binary} /usage exited {proc.returncode}: "
                f"{_tail(stderr) or _tail(stdout) or 'no output'}"
            ),
        )

    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        return ProbeResult(
            outcome=MALFORMED, ok=False, error=f"{binary} /usage output was not JSON: {exc}"
        )
    if not isinstance(payload, dict):
        return ProbeResult(
            outcome=MALFORMED, ok=False, error=f"{binary} /usage output was not a JSON object"
        )
    if payload.get("is_error"):
        return ProbeResult(
            outcome=CLI_ERROR,
            ok=False,
            error=f"{binary} /usage reported an error: {_tail_text(payload.get('result'))}",
        )

    result = payload.get("result")
    if not isinstance(result, str):
        return ProbeResult(
            outcome=MALFORMED, ok=False, error=f"{binary} /usage returned no result text"
        )

    parsed = parse_usage_text(result, now=observed_at)
    if parsed.unparsed:
        # The percentages are still whatever they were; only our reading of
        # the wording broke.  ``aq doctor --check providers.claude_usage``
        # is what turns this into a human's problem.
        logger.info("claude /usage output did not parse; keeping the previous reading")
        return ProbeResult(outcome=UNPARSED, unparsed=True)
    if not parsed.snapshots:
        return ProbeResult(outcome=NOT_APPLICABLE, detail="no subscription window to report")
    return ProbeResult(outcome=PROBED, snapshots=list(parsed.snapshots))


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill a probe that outlived its timeout and reap it.

    ``communicate`` was abandoned by ``wait_for``, so the pipes are still
    open; without the ``wait`` the child stays a zombie for the life of the
    daemon and a ten-minute timer accumulates them.
    """
    try:
        proc.kill()
    except ProcessLookupError:  # pragma: no cover - it already exited
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:  # pragma: no cover - defensive
        logger.debug("timed-out claude /usage probe did not reap")


def _tail(raw: bytes | None, limit: int = 200) -> str:
    return _tail_text(raw.decode("utf-8", "replace") if raw else None, limit)


def _tail_text(text: object, limit: int = 200) -> str:
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    return stripped[-limit:] if len(stripped) > limit else stripped
